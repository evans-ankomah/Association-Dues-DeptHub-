# syntax=docker/dockerfile:1

# --- Frontend assets -----------------------------------------------------------------
FROM node:22-alpine AS assets
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY scripts/vendor.mjs scripts/vendor.mjs
COPY assets/src assets/src
COPY templates templates
COPY apps apps
RUN npm run build

# --- Application ---------------------------------------------------------------------
FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod \
    PORT=8000
WORKDIR /app

RUN adduser --system --group --home /app depthub
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=depthub:depthub . .
COPY --from=assets --chown=depthub:depthub /build/static/css static/css
COPY --from=assets --chown=depthub:depthub /build/static/vendor static/vendor

# collectstatic needs settings to import; these values are build-time placeholders only.
RUN SECRET_KEY=build-only ALLOWED_HOSTS=localhost DATABASE_URL=postgres://x:x@localhost/x \
    python manage.py collectstatic --noinput

# Private document storage (mounted as a volume in production; owned by the app user).
RUN mkdir -p /app/private_media && chown depthub:depthub /app/private_media && chmod 750 /app/private_media

USER depthub
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz/',timeout=4)"

CMD ["sh", "-c", "gunicorn config.wsgi:application --bind 0.0.0.0:${PORT} --workers ${WEB_CONCURRENCY:-3} --access-logfile - --timeout 60"]
