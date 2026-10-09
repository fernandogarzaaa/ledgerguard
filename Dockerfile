# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

WORKDIR /srv
COPY pyproject.toml README.md LICENSE ./
COPY app ./app
RUN pip install . && useradd --create-home --uid 10001 ledgerguard && mkdir -p /srv/data \
    && chown ledgerguard /srv/data
USER ledgerguard

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD sh -c 'python -c "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=4)" "http://127.0.0.1:${PORT:-8000}/health"'

# Render injects $PORT; it defaults to 8000 locally.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
