# Pi Zero W - ARMv6
FROM balenalib/raspberry-pi:bullseye

# System deps
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    libjpeg62-turbo \
    libtiff5 \
    libopenjp2-7 \
    libxcb1 \
    zlib1g \
    libfreetype6 \
    liblcms2-2 \
    libwebp6 \
    libopenblas0 \
    libatlas3-base \
    udev \
    && rm -rf /var/lib/apt/lists/*

# USB rules for Wainlux K6 (CP210x serial)
RUN echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", MODE="0666"' > /etc/udev/rules.d/99-wainlux.rules

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip3 install --upgrade pip setuptools wheel && \
    pip3 install --no-cache-dir -r requirements.txt

# Copy k6 library
COPY k6/ ./k6/

# App
COPY app/ ./app/
COPY static/ ./static/
COPY templates/ ./templates/

EXPOSE 8080

CMD ["python3", "-m", "app.main"]
