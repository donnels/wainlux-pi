#!/usr/bin/env bash
# Quick deploy - assumes ssh-add already done
# Usage: PI_HOST=user@hostname ./quick_deploy.sh
set -euo pipefail

PI_HOST="${PI_HOST:-user@pi-ip}"

echo "=== Quick Deploy ==="
echo

# Just the essential commands
ssh $PI_HOST << 'COMMANDS'
set -e
cd ~/wainlux-pi/docker-wainlux
echo "[1/3] Stopping old container..."
docker compose down || true

echo "[2/3] Building (15-20 min on Pi Zero W)..."
docker compose build

echo "[3/3] Starting..."
docker compose up -d

echo
echo "Status:"
docker compose ps

echo
echo "Recent logs:"
docker compose logs --tail=30

echo
echo "=== Deployment Complete ==="
echo "Access: http://pi-ip:8080/"
COMMANDS
