#!/usr/bin/env bash
# Deploy to Pi Zero W - complete workflow
# Usage: PI_HOST=user@hostname ./deploy_to_pi.sh
set -euo pipefail

PI_HOST="${PI_HOST:-user@pi-ip}"
PI_DIR="${PI_DIR:-~/wainlux-pi}"
DOCKER_DIR="docker-wainlux"

echo "=== Wainlux K6 Docker Deployment ==="
echo "Target: ${PI_HOST}:${PI_DIR}"
echo

# Step 1: Sync code
echo "[1/5] Syncing code to Pi..."
tar czf /tmp/wainlux-deploy.tar.gz \
  --exclude='.git' \
  --exclude='venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='data' \
  .

scp /tmp/wainlux-deploy.tar.gz "${PI_HOST}:/tmp/"
ssh "${PI_HOST}" << EOF
  set -e
  mkdir -p ${PI_DIR}
  cd ${PI_DIR}
  tar xzf /tmp/wainlux-deploy.tar.gz 2>/dev/null || tar xzf /tmp/wainlux-deploy.tar.gz --warning=no-timestamp
  rm /tmp/wainlux-deploy.tar.gz
  mkdir -p docker-wainlux/data
  echo "Sync complete"
EOF
rm /tmp/wainlux-deploy.tar.gz

if [ $? -ne 0 ]; then
  echo "ERROR: Sync failed"
  exit 1
fi

echo
echo "[2/5] Checking Pi system..."
ssh "${PI_HOST}" << 'EOF'
  set -e
  echo "  Disk space:"
  df -h / | tail -1
  echo "  Swap:"
  free -h | grep Swap
  echo "  USB device:"
  lsusb | grep CP210 || echo "  WARNING: K6 not detected"
  echo "  Docker:"
  docker --version
EOF

if [ $? -ne 0 ]; then
  echo "WARNING: System check had issues"
fi

echo
echo "[3/5] Building Docker image (this takes 15-20 minutes on Pi Zero W)..."
ssh "${PI_HOST}" << EOF
  set -e
  cd ${PI_DIR}/${DOCKER_DIR}
  docker compose down || true
  docker compose build
EOF

if [ $? -ne 0 ]; then
  echo "ERROR: Docker build failed"
  exit 1
fi

echo
echo "[4/5] Starting service..."
ssh "${PI_HOST}" << EOF
  set -e
  cd ${PI_DIR}/${DOCKER_DIR}
  docker compose up -d
  sleep 3
  docker compose ps
EOF

if [ $? -ne 0 ]; then
  echo "ERROR: Failed to start service"
  exit 1
fi

echo
echo "[5/5] Checking logs..."
ssh "${PI_HOST}" << EOF
  cd ${PI_DIR}/${DOCKER_DIR}
  docker compose logs --tail=20
EOF

echo
echo "=== Deployment Complete ==="
echo
echo "Web interface: http://${PI_HOST##*@}:8080/"
echo
echo "To view logs: ssh ${PI_HOST} 'cd ${PI_DIR}/${DOCKER_DIR} && docker compose logs -f'"
echo "To stop:      ssh ${PI_HOST} 'cd ${PI_DIR}/${DOCKER_DIR} && docker compose down'"
echo
