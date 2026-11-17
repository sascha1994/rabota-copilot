FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && useradd --create-home --uid 10001 copilot \
    && mkdir -p /app/data && chown copilot:copilot /app/data
COPY app ./app
COPY search.example.json ./search.example.json
USER copilot
STOPSIGNAL SIGINT
CMD ["python", "-m", "app", "bot"]
