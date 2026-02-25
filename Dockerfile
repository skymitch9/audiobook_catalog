# Multi-stage Dockerfile for audiobook_catalog with integrated React frontend

# Stage 1: Build React frontend
FROM node:20-alpine AS frontend-builder

WORKDIR /app/frontend

# Copy frontend package files
COPY frontend/package*.json ./

# Install dependencies
RUN npm ci

# Copy frontend source
COPY frontend/ ./

# Build React app
RUN npm run build

# Stage 2: Python backend with Flask
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy Python requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY generate_stats.py .
COPY pyproject.toml .

# Copy built React frontend from builder stage
COPY --from=frontend-builder /app/frontend/dist ./site/build/

# Copy site directory (for archive and catalog.csv)
COPY site/ ./site/

# Expose Flask port
EXPOSE 5000

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=app.web.server
ENV FLASK_ENV=production

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

# Run Flask server
CMD ["python", "-m", "app.web.server"]
