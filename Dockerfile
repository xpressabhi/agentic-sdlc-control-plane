FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ASDLC_DATA_DIR=/data \
    ASDLC_DB_PATH=/data/agentic_sdlc.db \
    ASDLC_ARTIFACT_ROOT=/data/runs \
    ASDLC_SCENARIOS_DIR=/app/scenarios \
    ASDLC_ENV=development

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY scenarios ./scenarios

RUN pip install --no-cache-dir . \
    && useradd --create-home app \
    && mkdir -p /data \
    && chown -R app:app /data /app

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

CMD ["asdlc", "serve", "--host", "0.0.0.0", "--port", "8000"]
