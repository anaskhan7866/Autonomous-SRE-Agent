FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the agent codebase
COPY . .

# Expose the webhook listener port
EXPOSE 5000

# Start the webhook listener
CMD ["python", "api/webhook_listener.py"]