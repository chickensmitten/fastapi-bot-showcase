FROM python:3.11-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir -r requirements.txt

# Add a simple health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8080/ || exit 1

# Change port to match App Platform's expected default
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
