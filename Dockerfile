FROM python:3.12-slim

WORKDIR /app

# Build deps for cryptography package
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libffi-dev && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Railway provides PORT env var
ENV PORT=8000

EXPOSE ${PORT}

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
