# Multi-platform Flask app for K6 laser control
# Pi Zero W (armv6) + Steam Deck/dev (x86_64/amd64)
# See ADR-001-base-image.adoc for rationale
# Two-stage build: compile in builder, copy to runtime image

# ============================================================================
# STAGE 1: Builder - Compile Python packages
# ============================================================================
FROM python:3.13-slim AS builder

# Build dependencies for numpy/Pillow compilation
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    gfortran \
    cmake \
    autoconf \
    automake \
    libtool \
    pkg-config \
    libffi-dev \
    ninja-build \
    patchelf \
    libjpeg62-turbo-dev \
    libtiff-dev \
    libopenjp2-7-dev \
    libfreetype6-dev \
    liblcms2-dev \
    libwebp-dev \
    zlib1g-dev \
    libgfortran5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install Python packages to /build/venv
COPY requirements.txt .
RUN python -m venv /build/venv
ENV PATH="/build/venv/bin:$PATH"
ENV PIP_PREFER_BINARY=1
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# ============================================================================
# STAGE 2: Runtime - Clean image with only runtime deps
# ============================================================================
FROM python:3.13-slim

# Runtime dependencies (no build tools)
RUN apt-get update && apt-get install -y \
    libjpeg62-turbo \
    libtiff6 \
    libopenjp2-7 \
    libxcb1 \
    fonts-dejavu-core \
    zlib1g \
    libfreetype6 \
    liblcms2-2 \
    libwebp7 \
    libgfortran5 \
    udev \
    && rm -rf /var/lib/apt/lists/*

# Copy compiled Python packages from builder
COPY --from=builder /build/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# USB rules for Wainlux K6 (CP210x serial)
RUN echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", MODE="0666"' > /etc/udev/rules.d/99-wainlux.rules || true

WORKDIR /app

# Copy k6 library
COPY k6/ ./k6/

# Copy sample images
COPY images/ ./images/

# App (remove Python cache to force fresh compilation)
COPY app/ ./app/
RUN find /app -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
RUN find /app -type f -name '*.pyc' -delete 2>/dev/null || true

COPY static/ ./static/
COPY templates/ ./templates/
COPY settings/ ./settings/
COPY start_services.sh ./start_services.sh
RUN chmod +x /app/start_services.sh

# Prevent Python from writing pyc files
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Optional: Enable mock device mode (no hardware required)
ENV K6_MOCK_DEVICE=false

# Expose ports: Flask REST API (8080), MCP HTTP/SSE (8081)
EXPOSE 8080 8081

# Start Flask API + MCP HTTP server
CMD ["/app/start_services.sh"]
