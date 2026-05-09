# =====================================================
# Dockerfile multi-stage para Kalshi Bot
# Imagen final ~150MB
# =====================================================

# ---- Builder stage ----
FROM python:3.12-slim AS builder

WORKDIR /build

# Build deps (necesarias para cryptography compile en algunas plataformas)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libssl-dev \
        libffi-dev \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Instalar deps en venv aislado
COPY pyproject.toml .

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install --no-cache-dir \
        "httpx>=0.27.0" \
        "websockets>=12.0" \
        "pydantic>=2.6.0" \
        "pydantic-settings>=2.2.0" \
        "sqlmodel>=0.0.16" \
        "cryptography>=42.0.0" \
        "apscheduler>=3.10.4" \
        "python-telegram-bot>=21.0" \
        "loguru>=0.7.2" \
        "fastapi>=0.110.0" \
        "uvicorn[standard]>=0.29.0" \
        "tenacity>=8.2.3"

# ---- Runtime stage ----
FROM python:3.12-slim AS runtime

# Solo deps de runtime (curl para health check)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Usuario no-root para seguridad
RUN groupadd -r botuser --gid=1000 \
    && useradd -r -g botuser --uid=1000 --create-home --shell /bin/bash botuser

# Copiar venv del builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

# Copiar código (orden por frecuencia de cambio - cache friendly)
COPY --chown=botuser:botuser src/ ./src/
COPY --chown=botuser:botuser scripts/ ./scripts/
COPY --chown=botuser:botuser config/ ./config/

# Crear directorios para volúmenes (data, logs, secrets)
RUN mkdir -p /app/data /app/logs /app/secrets \
    && chown -R botuser:botuser /app

USER botuser

EXPOSE 8080

# Healthcheck nativo de Docker
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Punto de entrada: production runner
CMD ["python", "-m", "src.runner"]
