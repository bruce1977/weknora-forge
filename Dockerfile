FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FORGE_CONFIG=/data/config.json

WORKDIR /srv

COPY requirements.txt ./
RUN pip install -r requirements.txt && rm requirements.txt \
    && useradd --create-home --uid 10001 forge && mkdir -p /data && chown -R forge:forge /data

USER forge

# Copy the app as a proper package so relative imports (from . / from ..) keep working.
# `app.main:app` is the entrypoint (matches local dev: `python -m uvicorn app.main:app`).
COPY --chown=forge:forge app /srv/app
COPY --chown=forge:forge scripts /srv/scripts

# Ship the default config template (overridden at runtime by the mounted /data volume
# or by FORGE_CONFIG). Not mounted => app falls back to built-in defaults.
COPY --chown=forge:forge data /srv/data

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v2/health', timeout=2)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
