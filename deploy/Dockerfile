# Call QA: образ приложения (CPU-вариант). Контекст сборки: каталог ../app
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HOME=/models XDG_CACHE_HOME=/models/.cache HF_HOME=/models/hf CALLQA_DATA=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core git curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-сборка torch (как на стенде), затем замороженный набор (в нём GigaAM по коммиту и faster-whisper)
RUN pip install torch==2.11.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.lock .
RUN pip install -r requirements.lock

COPY . .

# шрифт с кириллицей для PDF-выгрузки
ENV CALLQA_FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf \
    CALLQA_FONT_BOLD=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf

RUN useradd -r -u 10001 callqa && mkdir -p /data /models && chown -R callqa /data /models /app
USER callqa

EXPOSE 8130
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8130/health || exit 1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8130", "--proxy-headers", "--forwarded-allow-ips", "*"]
