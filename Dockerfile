# LoanLens pipeline image. Generated data, artifacts and reports live in /workspace (mount it).
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 git \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-cache ".[orchestration,postgres]"
COPY config ./config
COPY dbt ./dbt

ENV LOANLENS_WORKSPACE=/workspace \
    LOANLENS_REPO=/app \
    DAGSTER_HOME=/workspace/.dagster \
    PYTHONUNBUFFERED=1
RUN mkdir -p /workspace/.dagster
VOLUME /workspace
ENTRYPOINT ["loanlens"]
CMD ["all"]
