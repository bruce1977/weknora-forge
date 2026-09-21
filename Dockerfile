# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

RUN useradd --create-home --uid 10001 forge && mkdir -p /data && chown -R forge:forge /data

COPY --chown=forge:forge app ./app
COPY --chown=forge:forge scripts ./scripts

USER forge
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" || exit 1

# --no-proxy-headers: uvicorn must NOT rewrite scheme / client address itself. Forge
# does it in app/proxy.py driven by the "proxy" section of config.json, which keeps a
# single source of truth and lets us honour CF-Connecting-IP. Uvicorn's own handling
# would only trust 127.0.0.1 by default, so a tunnel in another container (172.x)
# would silently be ignored and redirects would downgrade to http://.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
