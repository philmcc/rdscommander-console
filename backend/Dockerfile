# syntax=docker/dockerfile:1.7
FROM oven/bun:1.3 AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/bun.lock ./
RUN bun install --frozen-lockfile
COPY frontend/ ./
COPY contracts/ /contracts/
RUN bun run generate:api && bun run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS backend
ARG OPENTOFU_VERSION=1.13.1
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client curl unzip ca-certificates \
    && curl -fsSL "https://github.com/opentofu/opentofu/releases/download/v${OPENTOFU_VERSION}/tofu_${OPENTOFU_VERSION}_linux_${TARGETARCH}.zip" -o /tmp/tofu.zip \
    && unzip -q /tmp/tofu.zip tofu -d /usr/local/bin && rm /tmp/tofu.zip \
    && apt-get purge -y unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
    # The repository is a bind mount owned by the host user; git must not refuse it.
    && git config --system --add safe.directory '*' \
    # Operators' ~/.ssh and ~/.gitconfig are mounted read-only; git credentials never prompt.
    && git config --system credential.helper 'store --file /root/.git-credentials'

WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev
COPY --from=frontend /frontend/dist /app/frontend/dist
ENV PATH="/app/backend/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 RDSC_LISTEN_ON_ALL_INTERFACES_INSIDE_CONTAINER=true
EXPOSE 8470
CMD ["python", "-m", "rdscommander.main"]
