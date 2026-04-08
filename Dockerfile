FROM python:3.12-alpine

LABEL org.opencontainers.image.source="https://github.com/pcruz1905/hls-restream-proxy"
LABEL org.opencontainers.image.description="HLS restream proxy for Jellyfin/Emby/Plex"
LABEL org.opencontainers.image.licenses="MIT"

RUN adduser -D -h /app hlsproxy

WORKDIR /app
COPY hls-proxy.py .
COPY channels.conf.example .

USER hlsproxy

ENV HLS_PROXY_PORT=8089
ENV HLS_PROXY_BIND=0.0.0.0
ENV PYTHONUNBUFFERED=1

EXPOSE 8089

HEALTHCHECK --interval=30s --timeout=3s \
  CMD wget -qO- http://localhost:8089/health || exit 1

ENTRYPOINT ["python3", "hls-proxy.py"]
