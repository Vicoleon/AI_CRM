# Build Frontend
FROM node:20-alpine as frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
# The deployment's own Google OAuth web client id. A build arg with no default:
# no deployment's client is baked into the image.
ARG VITE_GOOGLE_CLIENT_ID
ENV VITE_GOOGLE_CLIENT_ID=$VITE_GOOGLE_CLIENT_ID
# The Google Cloud project NUMBER that holds the Meet add-on deployment (the
# Marketplace SDK config). The side panel passes it to createAddonSession and
# Meet refuses the session if it does not match. Same project as the OAuth
# client above; see docs/meet_addon/README.md. No default either.
ARG VITE_MEET_ADDON_CLOUD_PROJECT_NUMBER
ENV VITE_MEET_ADDON_CLOUD_PROJECT_NUMBER=$VITE_MEET_ADDON_CLOUD_PROJECT_NUMBER
RUN npm run build

# Build Backend
FROM python:3.10.13-slim as backend
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN echo "Cache bust 2026-04-23"
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Backend Code
COPY backend/ ./backend/
COPY rubik_core/ ./rubik_core/
COPY alembic.ini .
COPY alembic/ ./alembic/
COPY alembic_control.ini .
COPY alembic_control/ ./alembic_control/
COPY deploy/ ./deploy/

# Copy Frontend Build
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

# Serve Frontend Static Files Integration
# We need to tell FastAPI to serve these static files.
# Implementation: In backend/main.py, mount /app/frontend/dist to /
# Environment variables

ENV PORT=8080
ENV APP_MODE=gcp
ENV PYTHONUNBUFFERED=1

# Run
COPY scripts/start.sh /app/scripts/start.sh
RUN chmod +x /app/scripts/start.sh
CMD ["/app/scripts/start.sh"]
