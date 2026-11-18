#!/bin/sh
set -eu
umask 077
password_file=/app/data/vnc.passwd
if [ ! -s "$password_file" ]; then
    x11vnc -storepasswd "$password_file"
fi
exec x11vnc -display "$DISPLAY" -rfbauth "$password_file" -rfbport 5900 -once
