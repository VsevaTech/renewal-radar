FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 radar \
    && mkdir -p /srv/data \
    && chown -R radar:radar /srv
USER radar

ENV DATABASE_PATH=/srv/data/renewal_radar.db
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
