FROM python:3.11-slim

WORKDIR /app

# Ensure curl and sqlite3 are installed
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy app code
COPY . /app

# Create data directory with write permissions
RUN mkdir -p /app/data && chmod 777 /app/data

# Environment settings
ENV PYTHONUNBUFFERED=1 \
    ECELL_HOST=0.0.0.0 \
    ECELL_PORT=8000 \
    ECELL_TRUST_PROXY=1

EXPOSE 8000

# Health check
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/api/public/exam || exit 1

CMD ["python", "server.py"]
