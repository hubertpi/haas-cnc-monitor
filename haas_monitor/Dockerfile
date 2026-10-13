FROM python:3.12-slim-bookworm
ARG BUILD_VERSION=0.1.0
LABEL org.opencontainers.image.source="https://github.com/hubertpi/haas-cnc-monitor"
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="app" io.hass.arch="amd64"
ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/monitor
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.lock
COPY app/ ./app/
COPY web/ ./web/
EXPOSE 8099
STOPSIGNAL SIGTERM
CMD ["python", "-m", "app.main"]
