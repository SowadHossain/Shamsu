FROM node:22-bookworm-slim AS node-runtime
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PIP_NO_CACHE_DIR=1 \
    SHAMSU_HOME=/var/lib/shamsu SHAMSU_WORKSPACE_ROOT=/workspace \
    SHAMSU_WEB_HOST=0.0.0.0 SHAMSU_WEB_PORT=8765 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

# The agent can build Python/Node projects and inspect browser applications.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git build-essential tini nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/shamsu
COPY pyproject.toml README.md ./
RUN python -c "import subprocess,sys,tomllib; config=tomllib.load(open('pyproject.toml','rb')); subprocess.check_call([sys.executable,'-m','pip','install',*config['project']['dependencies']])"
RUN python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright \
    && useradd --create-home --uid 1000 shamsu \
    && mkdir -p /workspace /var/lib/shamsu \
    && chown -R shamsu:shamsu /workspace /var/lib/shamsu

COPY shamsu ./shamsu
RUN pip install --no-deps .

USER shamsu
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
USER root
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
USER shamsu
WORKDIR /workspace
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3)"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "shamsu.webui"]
