# syntax=docker/dockerfile:1

# ---------- Stage 1: build the React/Vite storefront ----------
# Flask serves the built SPA from storefront-ui/dist. We build it here so the final
# image contains the compiled assets and needs no Node at runtime.
FROM node:20-slim AS frontend
WORKDIR /ui
COPY storefront-ui/package.json storefront-ui/package-lock.json ./
RUN npm ci
COPY storefront-ui/ ./
RUN npm run build          # outputs to /ui/dist

# ---------- Stage 2: the Python app (waitress) ----------
# 3.12-slim has broad wheel availability for chromadb/onnxruntime; build-essential is
# insurance for any package without a prebuilt wheel.
FROM python:3.12-slim AS app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_APP=backend \
    FLASK_DEBUG=0

WORKDIR /app

# No apt layer: the required Python packages ship prebuilt manylinux wheels for cp312,
# and the healthcheck below uses Python (stdlib) instead of curl — so the image needs
# no system build tools and doesn't depend on the Debian mirrors at build time.

# Install Python deps first so this layer caches unless requirements change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source, then drop in the built SPA from the frontend stage.
COPY . .
COPY --from=frontend /ui/dist ./storefront-ui/dist

EXPOSE 5000

# Container liveness — hits the dependency-free probe (Python stdlib, no curl needed).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5000/healthz').getcode()==200 else 1)"

# Default: run the web tier under waitress. docker-compose overrides this command for
# the worker and beat services (same image, different process).
CMD ["waitress-serve", "--listen=*:5000", "wsgi:app"]
