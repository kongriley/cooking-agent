# The sandbox for Pepper's shopping browser: Chromium on a virtual display, its own profile volume, and a noVNC
# viewer for the one-time Instacart sign-in. The agent that drives it runs outside, over the DevTools port.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium xvfb x11vnc novnc websockify socat fonts-liberation fonts-noto-color-emoji ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home shopper && mkdir /profile && chown shopper /profile
COPY start.sh /usr/local/bin/start.sh
USER shopper

# 9223: DevTools (forwarded to Chromium's localhost-only 9222). 6080: noVNC viewer.
EXPOSE 9223 6080
CMD ["/usr/local/bin/start.sh"]
