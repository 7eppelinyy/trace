# Multi-stage Dockerfile for Trace Event Intelligence System (T14 / F22)
FROM python:3.11-slim-bookworm AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-core.lock .
RUN pip install --no-cache-dir --user --require-hashes -r requirements-core.lock

FROM python:3.11-slim-bookworm AS runner

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    sqlite3 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user setup for production security
RUN groupadd -g 1000 trace && \
    useradd -u 1000 -g trace -s /bin/bash -m trace

# Copy installed wheels/packages from builder
COPY --from=builder /root/.local /home/trace/.local
ENV PATH=/home/trace/.local/bin:$PATH

# Copy project code
COPY --chown=trace:trace trace/ ./trace/
COPY --chown=trace:trace scripts/ ./scripts/
COPY --chown=trace:trace requirements-core.lock .

# Prepare data and log directories with proper permissions
RUN mkdir -p /app/data /app/logs && chown -R trace:trace /app

USER trace

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    TRACE_DB_PATH=/app/data/trace.db \
    TRACE_BACKUP_DIR=/app/data/backups

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/ready || exit 1

CMD ["uvicorn", "trace.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
