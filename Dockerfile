FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright DISPLAY=:99 HOME=/home/copilot
WORKDIR /app
COPY requirements.lock requirements-browser.lock ./
RUN pip install --no-cache-dir -r requirements.lock -r requirements-browser.lock
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 xvfb x11-utils x11vnc fluxbox \
    && python -m playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*
RUN python -m playwright install --no-shell chromium
RUN useradd --create-home --uid 10001 --user-group copilot \
    && mkdir -p /app/data && chown copilot:copilot /app/data
COPY app ./app
COPY search.example.json ./search.example.json
COPY --chmod=755 deploy/start-bot.sh /usr/local/bin/start-bot
COPY --chmod=755 deploy/browser-view.sh /usr/local/bin/browser-view
USER copilot
STOPSIGNAL SIGINT
CMD ["start-bot"]
