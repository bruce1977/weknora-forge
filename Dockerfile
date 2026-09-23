FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FORGE_CONFIG=/data \
    PYTHONPATH=/

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt && rm requirements.txt \
    && useradd --create-home --uid 10001 forge && mkdir -p /data && chown -R forge:forge /data

USER forge

# The application package IS /app: app/__init__.py, app/main.py, ... land directly
# under /app. Because the code uses package-relative imports (`from .` / `from ..`),
# `app` must be imported as a package, so its PARENT (/, via PYTHONPATH=/) is on the
# path and `uvicorn app.main:app` resolves to /app. Startup runs from WORKDIR /app.
COPY --chown=forge:forge app /app

# Auxiliary helper scripts (HMAC signing, sample requests, config viewer).
# Standalone tools, NOT imported by the app at runtime.
COPY --chown=forge:forge scripts /opt/scripts

# Default config baked into the /data volume so the image runs standalone.
# At deployment, mount the host config directory over /data and config.json inside
# it is used (FORGE_CONFIG=/data points at the config *directory*).
COPY --chown=forge:forge data /data

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v2/health', timeout=2)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
