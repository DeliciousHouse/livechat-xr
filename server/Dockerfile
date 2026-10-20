# Build from the repo root:  docker build -f server/Dockerfile -t livechat-xr-relay .
FROM python:3.12-slim
WORKDIR /srv
RUN pip install --no-cache-dir TikTokLive==7.0.1 PyNaCl==1.6.0 tzdata==2026.5
COPY app/chat.py server/server.py server/network.py server/backup.py server/pretext.js ./
COPY assets/brand/web ./assets/brand/web
RUN mkdir /data && chown nobody /data
ENV DATA_DIR=/data PORT=13300
VOLUME /data
EXPOSE 13300
USER nobody
CMD ["python", "-u", "server.py"]
