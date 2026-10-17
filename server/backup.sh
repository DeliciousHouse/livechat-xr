#!/bin/sh
# Based on Dora's installed script, supplied read-only on 2026-10-17.
# Same daily filename, JSON/log backup, 30-day retention and success stamp.
# Install backup.py alongside this file; the reviewed image also contains it.
set -eu
umask 077
d="$HOME/backups/livechat-xr"
out="$d/livechat-xr-data-$(date +%F).tar.gz"
tmp="$out.tmp"
raw="$out.tar.tmp"
helper="$(dirname "$0")/backup.py"
mkdir -p "$d"
# Serialize the whole archive/check/publish operation, not only the snapshot.
exec 9>"$d/.backup.lock"
flock -n 9
cleanup() {
    result=$?
    rm -f "$tmp" "$raw"
    if [ "$result" -ne 0 ]; then
        printf 'FAILED %s\n' "$(date -Is)" > "$d/last-result"
    fi
}
trap cleanup 0
trap 'exit 1' HUP INT TERM

if docker ps --format '{{.Names}}' | grep -qx livechat-xr-relay; then
    docker exec livechat-xr-relay python /srv/backup.py archive /data > "$raw"
else
    # No network or pull. Snapshot needs a writable volume even with the relay stopped.
    docker run --rm --network none -v livechat-xr-data:/data --entrypoint python livechat-xr-relay /srv/backup.py archive /data > "$raw"
fi
# No pipe hides a failed docker/archive operation; raw contains sanitized tar only.
gzip -c "$raw" > "$tmp"
python3 "$helper" check-archive "$tmp"
mv "$tmp" "$out"
find "$d" \( -name 'livechat-xr-data-*.tar.gz' -o -name 'registrations-*.json' \) -mtime +30 -delete
if docker ps --format '{{.Names}}' | grep -qx livechat-xr-relay; then
    docker exec livechat-xr-relay sh -c 'date +%s > /data/last_backup'
else
    docker run --rm --network none -v livechat-xr-data:/data --entrypoint sh livechat-xr-relay -c 'date +%s > /data/last_backup'
fi
printf 'ok %s %s\n' "$(date -Is)" "$out" > "$d/last-result"
