# syntax=docker/dockerfile:1
FROM python:3.11-slim

WORKDIR /app

# CPU-only torch first, so sentence-transformers does not pull the CUDA build.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["python", "api/webhook_listener.py"]
