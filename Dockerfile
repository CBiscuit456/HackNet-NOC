# Use a lightweight python image
FROM python:3.11-slim

# Install the OS-level 'ping' utility required for network checks
RUN apt-get update && \
    apt-get install -y iputils-ping && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install dependencies first (for better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Expose the API port
EXPOSE 8000

# Default command (overridden by docker-compose for the collector)
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]

