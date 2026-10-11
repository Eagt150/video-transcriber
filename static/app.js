"use strict";

const $ = (id) => document.getElementById(id);
const ACTIVE = new Set(["queued", "extracting", "transcribing"]);
const STATUS_LABEL = {
  queued: "En cola", extracting: "Extrayendo", transcribing: "Transcribiendo",
  done: "Listo", error: "Error", cancelled: "Cancelado",
};
const VIDEO_EXT = /\.(mp4|m4v|webm|mov|mkv|avi|wmv|flv|3gp|ts|mts|mpg|mpeg|ogv)$/i;
const LANG_NAMES = new Intl.DisplayNames(["es"], { type: "language" });

const state = {
  config: null,
  jobs: [],
  selectedId: null,
  selected: null,        // detalle del trabajo seleccionado
  segments: [],
  uploading: false,
  uploadQueue: [],
};

// --------------------------------------------------------------------------
// Utilidades
// --------------------------------------------------------------------------

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  const u = ["KB", "MB", "GB", "TB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return `${n.toFixed(n < 10 ? 1 : 0)} ${u[i]}`;
}

function fmtTime(sec, withHours) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h || withHours ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function fmtDuration(sec) {
  if (sec == null) return "";
  sec = Math.round(sec);
  if (sec < 60) return `${sec} s`;
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}

function fmtEta(sec) {
  if (sec == null) return "";
  if (sec < 60) return "menos de 1 min";
  return `~${fmtDuration(sec)}`;
}

function langName(code) {
  try { return LANG_NAMES.of(code) || code; } catch { return code; }
}

let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3500);
}

function el(tag, props = {}, ...children) {
  const e = document.createElement(tag);
  Object.assign(e, props);
  for (const c of children) if (c != null) e.append(c);
  return e;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: opts.body && !(opts.body instanceof Blob) ? { "Content-Type": "application/json" } : undefined,
  });
  if (res.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("No autorizado");
  }
  let data = null;
  try { data = await res.json(); } catch { /* respuesta vacía */ }
  if (!res.ok) {
    const err = new Error((data && data.detail) || `Error ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

// --------------------------------------------------------------------------
// Arranque y sesión
// --------------------------------------------------------------------------

async function init() {
  state.config = await api("/api/config");
  if (state.config.auth_required && !state.config.authenticated) return showLogin();
  showApp();
}

function showLogin() {
  $("appView").hidden = true;
  $("logoutBtn").hidden = true;
  $("loginView").hidden = false;
  $("loginPassword").focus();
}

let appStarted = false;
function showApp() {
  $("loginView").hidden = true;
  $("appView").hidden = false;
  $("logoutBtn").hidden = !state.config.auth_required;
  if (appStarted) return;
  appStarted = true;

  const savedModel = localStorage.getItem("model");
  const savedLang = localStorage.getItem("language");
  const ms = $("modelSelect");
  for (const m of state.config.models) {
    ms.append(el("option", { value: m.id, textContent: m.id }));
  }
  ms.value = state.config.models.some((m) => m.id === savedModel) ? savedModel : state.config.default_model;
  const ls = $("languageSelect");
  for (const l of state.config.languages) ls.append(el("option", { value: l.id, textContent: l.label }));
  ls.value = savedLang && state.config.languages.some((l) => l.id === savedLang) ? savedLang : "auto";
  const hint = () => { $("modelHint").textContent = state.config.models.find((m) => m.id === ms.value)?.label || ""; };
  hint();
  ms.onchange = () => { localStorage.setItem("model", ms.value); hint(); };
  ls.onchange = () => localStorage.setItem("language", ls.value);

  const days = state.config.retention_hours / 24;
  $("retentionNote").textContent = `Se borran a los ${days >= 1 ? `${Math.round(days)} días` : `${state.config.retention_hours} h`}`;

  const hash = location.hash.slice(1);
  if (hash) state.selectedId = hash;
  poll();
}

$("loginForm").onsubmit = async (e) => {
  e.preventDefault();
  $("loginError").hidden = true;
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ password: $("loginPassword").value }) });
    state.config = await api("/api/config");
    showApp();
  } catch (err) {
    $("loginError").textContent = err.message;
    $("loginError").hidden = false;
  }
};

$("logoutBtn").onclick = async () => {
  await fetch("/api/logout", { method: "POST" });
  location.reload();
};

// --------------------------------------------------------------------------
// Subidas por trozos (reanudables)
// --------------------------------------------------------------------------

const dz = $("dropzone");
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
dz.addEventListener("dragleave", () => dz.classList.remove("over"));
dz.addEventListener("drop", (e) => {
  e.preventDefault();
  dz.classList.remove("over");
  addFiles(e.dataTransfer.files);
});
$("fileInput").onchange = (e) => { addFiles(e.target.files); e.target.value = ""; };

window.addEventListener("beforeunload", (e) => {
  if (state.uploading) { e.preventDefault(); e.returnValue = ""; }
});

function addFiles(fileList) {
  for (const file of fileList) {
    const item = {
      file,
      model: $("modelSelect").value,
      language: $("languageSelect").value,
      sent: 0,
      status: "Esperando…",
      cancelled: false,
      xhr: null,
    };
    item.node = renderUploadItem(item);
    $("uploadList").append(item.node.root);
    state.uploadQueue.push(item);
  }
  processUploads();
}

function renderUploadItem(item) {
  const bar = el("div");
  const status = el("span", { className: "muted" });
  const cancel = el("button", { className: "icon-btn", textContent: "Cancelar", type: "button" });
  cancel.onclick = () => cancelUpload(item);
  const root = el("li", { className: "upload-item" },
    el("div", { className: "row" }, el("span", { className: "name", textContent: item.file.name, title: item.file.name }), cancel),
    el("div", { className: "progress" }, bar),
    el("div", { className: "row" }, status, el("span", { className: "muted", textContent: fmtBytes(item.file.size) })),
  );
  return { root, bar, status, cancel };
}

function updateUploadItem(item) {
  item.node.bar.style.width = `${(item.sent / item.file.size) * 100}%`;
  item.node.status.textContent = item.status;
}

async function cancelUpload(item) {
  item.cancelled = true;
  if (item.xhr) item.xhr.abort();
  if (item.uploadId) {
    fetch(`/api/uploads/${item.uploadId}`, { method: "DELETE" });
    localStorage.removeItem(uploadKey(item.file));
  }
  item.node.root.remove();
}

const uploadKey = (f) => `upload:${f.name}:${f.size}:${f.lastModified}`;

async function processUploads() {
  if (state.uploading) return;
  state.uploading = true;
  try {
    while (state.uploadQueue.length) {
      const item = state.uploadQueue.shift();
      if (item.cancelled) continue;
      try {
        await uploadFile(item);
      } catch (err) {
        if (item.cancelled) continue;
        item.status = `Error: ${err.message}`;
        updateUploadItem(item);
        item.node.cancel.textContent = "Quitar";
        const retry = el("button", { className: "icon-btn", textContent: "Reintentar", type: "button" });
        retry.onclick = () => {
          retry.remove();
          item.node.cancel.textContent = "Cancelar";
          state.uploadQueue.push(item);
          processUploads();
        };
        item.node.cancel.before(retry);
      }
    }
  } finally {
    state.uploading = false;
  }
}

async function uploadFile(item) {
  const { file } = item;
  const key = uploadKey(file);
  let chunkSize = state.config.chunk_size;

  // ¿Hay una subida a medias de este mismo archivo? Se continúa donde quedó.
  item.uploadId = localStorage.getItem(key);
  if (item.uploadId) {
    try {
      const st = await api(`/api/uploads/${item.uploadId}`);
      item.sent = st.received;
      chunkSize = st.chunk_size;
    } catch {
      item.uploadId = null;
    }
  }
  if (!item.uploadId) {
    const up = await api("/api/uploads", { method: "POST", body: JSON.stringify({ filename: file.name, size: file.size }) });
    item.uploadId = up.id;
    item.sent = 0;
    chunkSize = up.chunk_size;
    localStorage.setItem(key, up.id);
  }

  const started = Date.now();
  const startedAt = item.sent;
  let failures = 0;

  while (item.sent < file.size) {
    if (item.cancelled) throw new Error("cancelado");
    const end = Math.min(item.sent + chunkSize, file.size);
    try {
      item.sent = await putChunk(item, file.slice(item.sent, end), item.sent, (loaded) => {
        const done = item.sent + loaded;
        const secs = (Date.now() - started) / 1000;
        const speed = (done - startedAt) / Math.max(secs, 0.1);
        const left = (file.size - done) / Math.max(speed, 1);
        item.status = `Subiendo ${Math.floor((done / file.size) * 100)}% · ${fmtBytes(speed)}/s · quedan ${fmtEta(left)}`;
        item.node.bar.style.width = `${(done / file.size) * 100}%`;
        item.node.status.textContent = item.status;
      });
      failures = 0;
    } catch (err) {
      if (item.cancelled) throw err;
      if (err.received != null) { item.sent = err.received; continue; } // el servidor indica dónde seguir
      if (err.fatal) throw err;
      failures++;
      if (failures > 8) throw new Error("se perdió la conexión repetidamente");
      const wait = Math.min(2 ** failures, 30);
      item.status = `Conexión interrumpida, reintentando en ${wait} s…`;
      updateUploadItem(item);
      await sleep(wait * 1000);
      try { item.sent = (await api(`/api/uploads/${item.uploadId}`)).received; } catch { /* se reintenta */ }
    }
  }

  item.status = "Enviando a la cola…";
  updateUploadItem(item);
  const job = await api(`/api/uploads/${item.uploadId}/complete`, {
    method: "POST",
    body: JSON.stringify({ model: item.model, language: item.language }),
  });
  localStorage.removeItem(key);
  item.node.root.remove();
  toast(`«${file.name}» subido. Transcripción en cola.`);
  if (!state.selectedId || !state.selected || !ACTIVE.has(state.selected.status)) selectJob(job.id);
  poll();
}

function putChunk(item, blob, offset, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    item.xhr = xhr;
    xhr.open("PUT", `/api/uploads/${item.uploadId}?offset=${offset}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => onProgress(e.loaded);
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* vacío */ }
      if (xhr.status === 200) return resolve(data.received);
      if (xhr.status === 401) { showLogin(); return reject(Object.assign(new Error("Sesión caducada"), { fatal: true })); }
      const err = new Error(data.detail || `Error ${xhr.status}`);
      if (data.received != null) err.received = data.received;
      if (xhr.status === 404 || xhr.status === 413 || xhr.status === 507) err.fatal = true;
      if (xhr.status === 404) localStorage.removeItem(uploadKey(item.file));
      reject(err);
    };
    xhr.onerror = () => reject(new Error("error de red"));
    xhr.onabort = () => reject(new Error("cancelado"));
    xhr.send(blob);
  });
}

// --------------------------------------------------------------------------
// Lista de trabajos y sondeo del servidor
// --------------------------------------------------------------------------

let pollTimer;
async function poll() {
  clearTimeout(pollTimer);
  try {
    state.jobs = await api("/api/jobs");
    renderJobs();
    if (state.selectedId) await refreshSelected();
  } catch (err) {
    if (err.message !== "No autorizado") console.warn(err);
  }
  const busy = state.jobs.some((j) => ACTIVE.has(j.status));
  pollTimer = setTimeout(poll, busy ? 1500 : 6000);
}

function jobSubline(j) {
  if (j.status === "queued") return "Esperando turno";
  if (j.status === "extracting") return `Extrayendo audio ${Math.round(j.progress * 100)}%`;
  if (j.status === "transcribing") {
    const p = Math.round(j.progress * 100);
    return j.stage === "Transcribiendo" ? `${p}%${j.eta_seconds != null ? ` · quedan ${fmtEta(j.eta_seconds)}` : ""}` : j.stage;
  }
  const parts = [];
  if (j.duration) parts.push(fmtDuration(j.duration));
  parts.push(j.model);
  if (j.detected_language || j.language) parts.push(langName(j.language || j.detected_language));
  if (j.status === "error") parts.push("ver detalle");
  return parts.join(" · ");
}

function renderJobs() {
  const list = $("jobList");
  list.replaceChildren();
  $("jobsEmpty").hidden = state.jobs.length > 0;
  for (const j of state.jobs) {
    const badgeClass = ACTIVE.has(j.status) && j.status !== "queued" ? "active" : j.status;
    const li = el("li", { className: `job${j.id === state.selectedId ? " active" : ""}`, tabIndex: 0 },
      el("div", { className: "row" },
        el("span", { className: "name", textContent: j.filename, title: j.filename }),
        el("span", { className: `badge ${badgeClass}`, textContent: STATUS_LABEL[j.status] || j.status }),
      ),
    );
    if (ACTIVE.has(j.status) && j.status !== "queued") {
      const bar = el("div");
      bar.style.width = `${j.progress * 100}%`;
      const indeterminate = j.status === "transcribing" && j.stage !== "Transcribiendo";
      li.append(el("div", { className: `progress${indeterminate ? " indeterminate" : ""}` }, bar));
    }
    li.append(el("span", { className: "sub", textContent: jobSubline(j) }));
    li.onclick = () => openJob(j.id);
    li.onkeydown = (e) => { if (e.key === "Enter") openJob(j.id); };
    list.append(li);
  }
}

// --------------------------------------------------------------------------
// Visor
// --------------------------------------------------------------------------

// En pantallas estrechas el visor queda debajo de la lista: se baja hasta él.
function openJob(id) {
  selectJob(id);
  if (matchMedia("(max-width: 900px)").matches) $("viewer").scrollIntoView({ behavior: "smooth" });
}

function selectJob(id) {
  if (state.selectedId === id && state.selected) return;
  state.selectedId = id;
  state.selected = null;
  state.segments = [];
  currentIdx = -1;
  history.replaceState(null, "", `#${id}`);
  $("transcript").replaceChildren();
  $("playerWrap").replaceChildren();
  $("searchInput").value = "";
  renderJobs();
  refreshSelected();
}

async function refreshSelected() {
  const id = state.selectedId;
  const prev = state.selected;
  if (prev && !ACTIVE.has(prev.status) && prev.segment_count === state.segments.length) {
    // Terminado y ya cargado: solo se actualiza si cambió el estado en la lista.
    const fromList = state.jobs.find((j) => j.id === id);
    if (!fromList) return clearViewer();
    if (fromList.status === prev.status) return;
  }
  let job;
  try {
    job = await api(`/api/jobs/${id}?since=${state.segments.length}`);
  } catch (err) {
    if (err.status === 404) clearViewer();
    return;
  }
  if (id !== state.selectedId) return;
  if (job.since !== state.segments.length) return; // respuesta desfasada
  state.selected = job;
  const isNew = !prev;
  if (job.segments.length) {
    state.segments.push(...job.segments);
    appendSegments(job.segments);
  }
  renderViewer(job, isNew);
}

function clearViewer() {
  state.selectedId = null;
  state.selected = null;
  state.segments = [];
  history.replaceState(null, "", location.pathname);
  $("viewerContent").hidden = true;
  $("viewerEmpty").hidden = false;
  renderJobs();
}

function renderViewer(job, isNew) {
  $("viewerEmpty").hidden = true;
  $("viewerContent").hidden = false;
  $("vTitle").textContent = job.filename;

  const meta = [fmtBytes(job.size)];
  if (job.duration) meta.push(fmtDuration(job.duration));
  meta.push(`modelo ${job.model}`);
  if (job.language) meta.push(langName(job.language));
  else if (job.detected_language) meta.push(`${langName(job.detected_language)} (detectado)`);
  if (job.status === "done" && job.started_at && job.finished_at) {
    meta.push(`procesado en ${fmtDuration(job.finished_at - job.started_at)}`);
  }
  $("vMeta").textContent = meta.join(" · ");

  const active = ACTIVE.has(job.status);
  $("vStatus").hidden = !active;
  if (active) {
    let stage = job.stage, note = "";
    if (job.status === "queued") {
      stage = "En cola";
      note = job.queue_position > 1 ? `Hay ${job.queue_position - 1} archivo(s) antes que este.` : "Empezará en breve.";
    } else if (job.status === "transcribing" && job.stage === "Transcribiendo") {
      note = job.eta_seconds != null ? `Tiempo restante estimado: ${fmtEta(job.eta_seconds)}. Puedes cerrar esta página; el trabajo sigue en el servidor.` : "Calculando tiempo restante…";
    } else if (job.status === "transcribing") {
      note = "Si es la primera vez que se usa este modelo, se está descargando; puede tardar unos minutos.";
    }
    $("vStage").textContent = stage;
    const showPct = job.status !== "queued" && !(job.status === "transcribing" && job.stage !== "Transcribiendo");
    $("vPercent").textContent = showPct ? `${Math.round(job.progress * 100)}%` : "";
    $("vBar").style.width = `${job.progress * 100}%`;
    $("vBar").parentElement.classList.toggle("indeterminate", !showPct);
    $("vStatusNote").textContent = note;
  }

  $("vError").hidden = job.status !== "error" && job.status !== "cancelled";
  if (job.status === "error") $("vError").textContent = `No se pudo transcribir: ${job.error}`;
  if (job.status === "cancelled") $("vError").textContent = "Transcripción cancelada. Se muestra lo que alcanzó a transcribirse.";

  renderActions(job);
  if (isNew) renderPlayer(job);

  const empty = $("transcriptEmpty");
  empty.hidden = state.segments.length > 0;
  if (!state.segments.length) {
    empty.textContent = active ? "El texto irá apareciendo aquí a medida que se transcriba." :
      job.status === "done" ? "No se detectó habla en este archivo." : "";
  }
}

function renderActions(job) {
  const has = state.segments.length > 0;
  $("copyBtn").disabled = !has;
  $("downloadBtn").disabled = !has;
  const actions = document.querySelector(".viewer-actions");
  let extra = $("jobExtraBtn");
  if (!extra) {
    extra = el("button", { id: "jobExtraBtn", className: "btn small" });
    actions.prepend(extra);
  }
  if (ACTIVE.has(job.status)) {
    extra.textContent = "Cancelar";
    extra.onclick = async () => {
      if (!confirm("¿Cancelar esta transcripción?")) return;
      await api(`/api/jobs/${job.id}/cancel`, { method: "POST" });
      poll();
    };
  } else {
    extra.textContent = "Eliminar";
    extra.onclick = async () => {
      if (!confirm("¿Eliminar esta transcripción y su archivo? No se puede deshacer.")) return;
      await api(`/api/jobs/${job.id}`, { method: "DELETE" });
      clearViewer();
      poll();
    };
  }
}

function renderPlayer(job) {
  const wrap = $("playerWrap");
  if (job.has_media === false) {
    wrap.replaceChildren(el("p", { className: "muted note", textContent:
      "El archivo original se borra al terminar para dejar espacio libre. La transcripción y las descargas siguen disponibles." }));
    return;
  }
  const isVideo = VIDEO_EXT.test(job.filename);
  const media = el(isVideo ? "video" : "audio", { controls: true, preload: "metadata", src: `/api/jobs/${job.id}/media` });
  media.playsInline = true;
  media.onerror = () => {
    wrap.replaceChildren(el("p", { className: "muted note", textContent:
      "No se puede reproducir el archivo (formato no compatible o ya se borró al terminar), pero la transcripción funciona igual." }));
  };
  media.ontimeupdate = () => highlightCurrent(media.currentTime);
  wrap.replaceChildren(media);
}

function appendSegments(segs) {
  const ol = $("transcript");
  const frag = document.createDocumentFragment();
  const withHours = (state.selected?.duration || segs[segs.length - 1].end) >= 3600;
  const q = $("searchInput").value.trim().toLowerCase();
  for (const s of segs) {
    const li = el("li", { className: "seg" },
      el("time", { textContent: fmtTime(s.start, withHours) }),
      el("p", { textContent: s.text }),
    );
    li.dataset.start = s.start;
    li.onclick = () => seek(s.start);
    if (q) applySearch(li, s.text, q);
    frag.append(li);
  }
  ol.append(frag);
  $("transcriptEmpty").hidden = true;
}

function seek(t) {
  const media = $("playerWrap").querySelector("video,audio");
  if (!media) return;
  media.currentTime = t;
  media.play().catch(() => {});
}

let currentIdx = -1;
function highlightCurrent(t) {
  const segs = state.segments;
  let lo = 0, hi = segs.length - 1, idx = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (segs[mid].start <= t) { idx = mid; lo = mid + 1; } else hi = mid - 1;
  }
  if (idx === currentIdx) return;
  const items = $("transcript").children;
  if (items[currentIdx]) items[currentIdx].classList.remove("current");
  currentIdx = idx;
  if (items[idx]) {
    items[idx].classList.add("current");
    if ($("followToggle").checked) items[idx].scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

// Búsqueda
function applySearch(li, text, q) {
  const p = li.querySelector("p");
  const lower = text.toLowerCase();
  if (!q) { p.textContent = text; li.hidden = false; return; }
  let i = lower.indexOf(q);
  li.hidden = i < 0;
  if (i < 0) { p.textContent = text; return; }
  p.replaceChildren();
  let last = 0;
  while (i >= 0) {
    p.append(text.slice(last, i), el("mark", { textContent: text.slice(i, i + q.length) }));
    last = i + q.length;
    i = lower.indexOf(q, last);
  }
  p.append(text.slice(last));
}

$("searchInput").oninput = () => {
  const q = $("searchInput").value.trim().toLowerCase();
  const items = $("transcript").children;
  state.segments.forEach((s, i) => items[i] && applySearch(items[i], s.text, q));
};

// Copiar y descargar
$("copyBtn").onclick = async () => {
  try {
    const res = await fetch(`/api/jobs/${state.selectedId}/download/txt`);
    await navigator.clipboard.writeText(await res.text());
    toast("Texto copiado al portapapeles");
  } catch {
    toast("No se pudo copiar el texto");
  }
};

$("downloadBtn").onclick = (e) => {
  e.stopPropagation();
  const menu = $("downloadMenu");
  menu.hidden = !menu.hidden;
  $("downloadBtn").setAttribute("aria-expanded", String(!menu.hidden));
};
document.addEventListener("click", () => { $("downloadMenu").hidden = true; });
$("downloadMenu").onclick = (e) => {
  const fmt = e.target.dataset.fmt;
  if (!fmt) return;
  location.href = `/api/jobs/${state.selectedId}/download/${fmt}`;
};

init().catch((err) => {
  document.body.append(el("p", { className: "error-box", textContent: `No se pudo conectar con el servidor: ${err.message}` }));
});
