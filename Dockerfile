FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PROXY_PORT=5000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxyseerr ./proxyseerr

RUN useradd --create-home --uid 1000 proxyseerr
USER proxyseerr

EXPOSE 5000

HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
url='http://127.0.0.1:%s/proxy/health' % os.environ.get('PROXY_PORT','5000'); \
sys.exit(0 if urllib.request.urlopen(url, timeout=5).status == 200 else 1)"

CMD ["python", "-m", "proxyseerr"]
