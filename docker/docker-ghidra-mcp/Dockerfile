# Local wrapper for the pinned upstream PyGhidra MCP image.
FROM ghcr.io/clearbluejar/pyghidra-mcp@sha256:51e200eeb562e06ea2c509049e4d363d99dbbfb7dff918a80171416b61545e32

COPY docker/start-ghidra-mcp.sh /usr/local/bin/start-ghidra-mcp
ENTRYPOINT ["/usr/local/bin/start-ghidra-mcp"]
