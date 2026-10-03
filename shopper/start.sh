#!/bin/sh
# Virtual display sized to the agent's screenshots, then Chromium, the DevTools forwarder, and the viewer.
set -e
export DISPLAY=:99
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
sleep 1
# The container is the sandbox, so Chromium's own sandbox (which needs extra kernel privileges) is off.
chromium --no-sandbox --no-first-run --no-default-browser-check --disable-dev-shm-usage \
    --user-data-dir=/profile --window-position=0,0 --window-size=1280,800 \
    --remote-debugging-port=9222 https://www.instacart.com/ &
# Chromium only listens on the container's localhost; forward it so the published port reaches it.
socat TCP-LISTEN:9223,fork,reuseaddr TCP:127.0.0.1:9222 &
x11vnc -display :99 -forever -shared -nopw -localhost -quiet &
exec websockify --web /usr/share/novnc 6080 localhost:5900
