FROM python:3.9-slim

WORKDIR /app

# Install ffmpeg which is required for moviepy
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first
COPY requirements.txt /app/

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir moviepy==1.0.3 decorator imageio imageio-ffmpeg proglog

# Copy application code
COPY . /app

# Make entrypoint script executable
RUN chmod +x entrypoint.sh

EXPOSE 8080

# Use the entrypoint script
ENTRYPOINT ["./entrypoint.sh"]