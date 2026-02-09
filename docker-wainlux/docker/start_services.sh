#!/bin/sh
set -eu

python3 app/main.py &
FLASK_PID=$!
echo "[supervisor] Flask started (pid=${FLASK_PID})"

python3 app/mcp_server.py &
MCP_PID=$!
echo "[supervisor] MCP started (pid=${MCP_PID})"

cleanup() {
  kill "$FLASK_PID" "$MCP_PID" 2>/dev/null || true
}

trap cleanup INT TERM

# Exit container if either process dies, but report the failing service first.
while :; do
  if ! kill -0 "$FLASK_PID" 2>/dev/null; then
    FLASK_STATUS=0
    wait "$FLASK_PID" || FLASK_STATUS=$?
    echo "[supervisor] Flask exited (pid=${FLASK_PID}, status=${FLASK_STATUS})"
    break
  fi

  if ! kill -0 "$MCP_PID" 2>/dev/null; then
    MCP_STATUS=0
    wait "$MCP_PID" || MCP_STATUS=$?
    echo "[supervisor] MCP exited (pid=${MCP_PID}, status=${MCP_STATUS})"
    break
  fi

  sleep 1
done

cleanup
wait "$FLASK_PID" 2>/dev/null || true
wait "$MCP_PID" 2>/dev/null || true
exit 1
