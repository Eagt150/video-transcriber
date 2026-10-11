FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    MODELS_DIR=/models

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# El modelo por defecto va dentro de la imagen: el volumen /data queda solo para
# los archivos y transcripciones, y el arranque no depende de descargarlo.
ARG WHISPER_MODEL=small
RUN python -c "from faster_whisper.utils import download_model; download_model('${WHISPER_MODEL}', cache_dir='/models')"

COPY app ./app
COPY static ./static

# Railway define $PORT. Montar un volumen en /data para conservar trabajos y
# modelos descargados entre despliegues.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
