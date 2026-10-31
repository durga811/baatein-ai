# The whole app in one image: the web page and API (`baatein serve`) and the voice agent
# (`baatein-agent start`), sharing one SQLite file on a volume at /app/data.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
WORKDIR /app

# Dependencies first, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# The models the agent loads for every call, baked in so the first call isn't slow.
RUN baatein-agent download-files

COPY deploy/start.sh ./deploy/start.sh

ENV BAATEIN_DATABASE__URL=sqlite:////app/data/baatein.db PORT=8000
EXPOSE 8000
CMD ["deploy/start.sh"]
