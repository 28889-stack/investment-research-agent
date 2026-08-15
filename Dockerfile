FROM node:22-bookworm-slim AS bridge-build
WORKDIR /build/pi_bridge
COPY pi_bridge/package.json pi_bridge/package-lock.json ./
RUN npm ci
COPY pi_bridge/tsconfig.json ./
COPY pi_bridge/src ./src
RUN npm run build && npm prune --omit=dev

FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/app/data/huggingface \
    PATH=/usr/local/bin:$PATH
WORKDIR /app
COPY --from=bridge-build /usr/local/bin/node /usr/local/bin/node
RUN apt-get update \
    && apt-get install -y --no-install-recommends git libatomic1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --create-home appuser
COPY requirements.txt requirements-kronos.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-kronos.txt
COPY config/kronos-source.json ./config/kronos-source.json
RUN mkdir -p /app/vendor \
    && git clone --filter=blob:none https://github.com/shiyu-coder/Kronos.git /app/vendor/kronos \
    && git -C /app/vendor/kronos fetch origin 67b630e67f6a18c9e9be918d9b4337c960db1e9a --depth=1 \
    && git -C /app/vendor/kronos checkout --detach 67b630e67f6a18c9e9be918d9b4337c960db1e9a \
    && rm -rf /app/vendor/kronos/.git
COPY app ./app
COPY scripts ./scripts
COPY --from=bridge-build /build/pi_bridge/dist ./pi_bridge/dist
COPY --from=bridge-build /build/pi_bridge/node_modules ./pi_bridge/node_modules
COPY --from=bridge-build /build/pi_bridge/package.json ./pi_bridge/package.json
RUN mkdir -p /app/data/artifacts /app/logs /app/backups /home/appuser/.cache \
    && chown -R appuser:appuser /app /home/appuser/.cache
USER appuser
EXPOSE 8000
CMD ["python", "-m", "app.service"]
