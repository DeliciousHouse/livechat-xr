# Build from the repo root:  docker build -f server/Dockerfile -t livechat-xr-relay .
FROM python:3.12-slim
WORKDIR /srv
COPY app/chat.py server/server.py server/network.py server/backup.py server/pretext.js ./
COPY assets/brand/web ./assets/brand/web
# Pins live in server/requirements.txt so GitHub's dependency graph and Dependabot alerts can see them.
COPY server/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
RUN mkdir /data && chown nobody /data
ENV DATA_DIR=/data PORT=13300
VOLUME /data
EXPOSE 13300
USER nobody
CMD ["python", "-u", "server.py"]
