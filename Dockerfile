# The web UI as a container; the live demo runs it on Railway (docs/DEPLOY.md), listening on PORT (7860).
# Read only: it serves stored results from a FalkorDB the settings point at, and never spends
# API credits or model calls. Settings come from the host's secrets, never from a file in the image.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --python /usr/local/bin/python3.12
COPY shaky ./shaky
COPY web ./web

RUN useradd -m -u 1000 app && chown -R app /app
USER app
ENV HOST=0.0.0.0 PORT=7860 ALLOW_RUNS=0 PYTHONUNBUFFERED=1
EXPOSE 7860
CMD [".venv/bin/python", "-m", "shaky.web"]
