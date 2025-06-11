# Use Python 3.12 slim image as base with explicit platform
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Set timezone
ENV TZ=Asia/Singapore

# Install uv using the official installer
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Copy project files
COPY . .

# Install Python dependencies using uv
RUN uv pip install --system .

# Create directory for session files
RUN mkdir -p /app/data

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Command to run the application
CMD ["uv", "run", "main.py"] 