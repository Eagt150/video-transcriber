# Transcriptor de Videos

Página web para transcribir videos y audios de cualquier tamaño y duración con
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (Whisper de OpenAI,
optimizado). Todo se procesa en el propio servidor: no se usan APIs de pago.

## Funciones

- Acepta cualquier formato de video o audio (MP4, MKV, MOV, AVI, WEBM, MP3, WAV,
  M4A, OGG, OPUS, FLAC…). La decodificación usa FFmpeg a través de PyAV, así que
  no hace falta instalar ffmpeg aparte.
- Subida por trozos y reanudable: si se corta la conexión, continúa donde quedó.
- La RAM no crece con la duración: el audio se procesa por ventanas de 20 minutos,
  cortadas en silencios para no partir palabras.
- Detección automática del idioma.
- Texto en vivo mientras se transcribe, con progreso y tiempo restante.
- Visor con reproductor mientras se transcribe: al hacer clic en una frase el video salta a ese momento. Al terminar, el archivo original se borra para liberar espacio (el texto se conserva).
- Búsqueda dentro de la transcripción.
- Descargas en TXT, TXT con tiempos, SRT, VTT y JSON.
- Cola de trabajos que sobrevive a reinicios del servidor.
- Privado por navegador: cada persona solo ve sus transcripciones, sin cuentas
  (una cookie anónima identifica al navegador).
- Contraseña opcional para cuando esté publicada en internet.
- Borrado automático de trabajos antiguos.

## Uso en local

Requisitos: Python 3.12 (se recomienda [uv](https://docs.astral.sh/uv/)).

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env    # opcional
./run.sh                # http://localhost:8000
```

La primera vez que se usa cada modelo se descarga (de ~75 MB para `tiny` a
~3 GB para `large-v3`) en `data/models/`.

## Modelos y velocidad

En la CPU de desarrollo (8 núcleos, sin GPU), el modelo `small` transcribe unas
2,5 veces más rápido que la duración real del audio, así que 1 hora de audio tarda
unos 25 minutos. Los modelos más grandes son más precisos y más lentos.

| Modelo | Precisión | Velocidad relativa |
|---|---|---|
| tiny | baja | muy rápido |
| base | básica | rápido |
| small | buena (recomendado en CPU) | media |
| medium | muy buena | lento |
| large-v3-turbo | excelente | lento en CPU |
| large-v3 | la mejor | muy lento en CPU |

## Configuración

Todas las opciones se definen con variables de entorno; ver `.env.example`.

## Estructura

```
app/
  main.py         rutas web y API, autenticación
  uploads.py      subidas por trozos reanudables
  jobs.py         cola de trabajos persistida en disco
  transcriber.py  extracción de audio y transcripción por ventanas
  formats.py      exportación a TXT/SRT/VTT/JSON
static/           interfaz web (HTML, CSS y JS sin dependencias)
Dockerfile        imagen para el despliegue (Railway)
```

## Despliegue (pendiente)

1. Subir el repositorio a GitHub.
2. En Railway, crear el servicio desde el repositorio (usa el `Dockerfile`).
3. Añadir un **volumen** montado en `/data` para conservar los trabajos y los
   modelos entre despliegues.
4. Definir `APP_PASSWORD` y `SESSION_SECRET`.
5. Conectar el dominio de Cloudflare. Mantener `CHUNK_SIZE_MB` por debajo de 100,
   porque Cloudflare rechaza peticiones mayores en el plan gratuito.
