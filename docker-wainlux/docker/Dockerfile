# Multi-platform Flask app for K6 laser control
# Pi Zero W (armv6) + Steam Deck/dev (x86_64/amd64)
# See ADR-001-base-image.adoc for rationale

FROM balenalib/raspberry-pi:bullseye

# System deps
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    libjpeg62-turbo \
    libtiff5 \
    libopenjp2-7 \
    libxcb1 \
    fonts-dejavu-core \
    zlib1g \
    libfreetype6 \
    liblcms2-2 \
    libwebp6 \
    libopenblas0 \
    libatlas3-base \
    udev \
    && rm -rf /var/lib/apt/lists/*

# Create python3.11 symlink for compatibility (actual version is 3.9)
RUN ln -sf /usr/bin/python3 /usr/bin/python

# USB rules for Wainlux K6 (CP210x serial) - optional, graceful if missing
RUN echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", MODE="0666"' > /etc/udev/rules.d/99-wainlux.rules || true

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip3 install --upgrade pip setuptools wheel && \
    pip3 install --no-cache-dir -r requirements.txt

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

# Prevent Python from writing pyc files
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Optional: Enable mock device mode (no hardware required)
ENV K6_MOCK_DEVICE=false

EXPOSE 8080

CMD ["python3", "-m", "app.main"]

