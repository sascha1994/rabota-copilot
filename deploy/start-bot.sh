#!/bin/sh
set -eu
umask 077
if [ "$#" -eq 0 ]; then
    set -- python -m app bot
fi
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix
Xvfb "$DISPLAY" -screen 0 1440x900x24 -nolisten tcp &
display_pid=$!
for attempt in $(seq 1 50); do
    if xdpyinfo >/dev/null 2>&1; then
        fluxbox >/tmp/fluxbox.log 2>&1 &
        exec "$@"
    fi
    kill -0 "$display_pid"
    sleep 0.2
done
echo "Xvfb failed to start on $DISPLAY" >&2
exit 1
