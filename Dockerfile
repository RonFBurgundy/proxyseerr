FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SONARR_PROXY_PORT=5000 \
    RADARR_PROXY_PORT=5001

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxyseerr ./proxyseerr

RUN useradd --create-home --uid 1000 proxyseerr
USER proxyseerr

EXPOSE 5000 5001

HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD ["python", "-m", "proxyseerr.healthcheck"]

CMD ["python", "-m", "proxyseerr"]
