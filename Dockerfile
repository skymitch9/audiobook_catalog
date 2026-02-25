# Multi-stage Dockerfile for audiobook_catalog with integrated React frontend

# Stage 1: Build React frontend
FROM node:20-alpine AS frontend-builder

WORKDIR /app/frontend

# Copy package files and install dependencies
COPY frontend/package*.json ./
RUN npm ci

# Copy source and build
COPY frontend/ ./
RUN npm run build

# Stage 2: Python backend with Flask (slim production image)
FROM python:3.11-slim

WORKDIR /app

# Install only curl for healthcheck, clean up in same layer
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Copy and install only production Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir \
    flask>=3.0.0 \
    flask-cors>=4.0.0 \
    && rm -rf /root/.cache/pip

# Copy only necessary application code
COPY app/web/ ./app/web/
COPY app/__init__.py ./app/__init__.py

# Copy built React frontend from builder stage
COPY --from=frontend-builder /app/frontend/dist ./site/build/

# Copy site directory structure (will be populated at runtime)
RUN mkdir -p ./site/archive ./site/covers

# Expose Flask port
EXPOSE 5000

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    FLASK_APP=app.web.server \
    FLASK_ENV=production \
    PYTHONDONTWRITEBYTECODE=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/ || exit 1

# Run Flask server
CMD ["python", "-m", "app.web.server"]
