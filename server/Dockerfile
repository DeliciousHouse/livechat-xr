# Build from the repo root:  docker build -f server/Dockerfile -t livechat-xr-relay .
FROM python:3.12-slim
WORKDIR /srv
RUN pip install --no-cache-dir TikTokLive==7.0.1
COPY app/chat.py server/server.py ./
RUN mkdir /data && chown nobody /data
ENV DATA_DIR=/data PORT=13300
VOLUME /data
EXPOSE 13300
USER nobody
CMD ["python", "-u", "server.py"]
