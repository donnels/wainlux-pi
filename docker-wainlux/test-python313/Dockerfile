# TEST: python:3.13-slim on ARMv6 (Pi Zero W)
# Testing before switching from balenalib base

FROM python:3.13-slim

# System deps (updated for Debian trixie package names)
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
    libopenblas0-pthread \
    libatlas3-base \
    udev \
    && rm -rf /var/lib/apt/lists/*

# USB rules for K6
RUN echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", MODE="0666"' > /etc/udev/rules.d/99-wainlux.rules || true

WORKDIR /app

# Python deps (with MCP dependencies enabled)
COPY requirements.txt .
RUN pip3 install --upgrade pip setuptools wheel && \
    pip3 install --no-cache-dir -r requirements.txt

# Test entry point
CMD ["python", "-c", "import flask; import PIL; import numpy; import serial; import qrcode; print('All imports successful'); import sys; print(f'Python {sys.version}')"]
