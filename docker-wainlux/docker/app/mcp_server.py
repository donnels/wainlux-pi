"""
MCP Server for K6 Laser Control - HTTP Client Architecture

Thin HTTP client adapter that calls Flask REST API.
Flask API owns hardware/serial access exclusively.

Network Architecture:
  Steam Deck (MCP client) ──HTTP──> Pi MCP server :8081 
                                     └─HTTP─> Flask API :8080 
                                              └─> K6 hardware (/dev/ttyUSB0)

Why This Architecture:
  - Single hardware owner (Flask API only)
  - No serial port contention
  - DRY: One place for device logic
  - MCP can run anywhere (distributed-ready)
  - Simplifies MCP: just HTTP client, no device_manager

Purpose:
  - AI-assisted testing via existing Flask endpoints
  - Same tool interface as before
  - Backend: HTTP calls to localhost:8080

SECURITY WARNING: Currently NO AUTHENTICATION.
For POC/testing only. Localhost-only binding.

Port: 8081 (calls Flask on 8080)
"""

import json
import logging
import os
import base64
import io
from pathlib import Path
from typing import Optional, Dict, Any
import httpx

from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource
try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - compatibility fallback for older SDKs
    FastMCP = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask API base URL (localhost by default; can be overridden for distributed setups)
FLASK_API_BASE = os.getenv("FLASK_API_BASE", "http://localhost:8080")

# Data directory for logs
DATA_DIR = Path(__file__).parents[1] / "data"
DATA_DIR.mkdir(exist_ok=True)

# MCP server instance
app = Server("k6-laser-mcp")
mcp_http = FastMCP("k6-laser-mcp") if FastMCP else None

logger.info("=== K6 MCP Server Starting (HTTP Client Mode) ===")
logger.info(f"Architecture: MCP → Flask API → K6 hardware")
logger.info(f"Flask API: {FLASK_API_BASE}")
logger.info(f"Port: 8081")
logger.info(f"Hardware owner: Flask API only (no serial contention)")
logger.info("==================================================")

_api_client: Optional[httpx.AsyncClient] = None


def _tool_text(payload: dict) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


def _tool_error(message: str, *, hint: Optional[str] = None, details: Optional[dict] = None) -> list[TextContent]:
    payload = {"success": False, "error": message}
    if hint:
        payload["hint"] = hint
    if details:
        payload["details"] = details
    return _tool_text(payload)


async def _get_api_client() -> httpx.AsyncClient:
    global _api_client
    if _api_client is None:
        _api_client = httpx.AsyncClient(
            base_url=FLASK_API_BASE,
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _api_client


async def _close_api_client() -> None:
    global _api_client
    if _api_client is not None:
        await _api_client.aclose()
        _api_client = None


async def _api_json(
    method: str,
    path: str,
    *,
    json_body: Optional[dict] = None,
    files: Optional[dict] = None,
    timeout: Optional[float] = None,
) -> dict:
    client = await _get_api_client()
    response = await client.request(method, path, json=json_body, files=files, timeout=timeout)

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Invalid JSON response from Flask API: {path}") from exc

    if not response.is_success:
        message = data.get("error") if isinstance(data, dict) else None
        if not message:
            message = f"HTTP {response.status_code} from {path}"
        raise RuntimeError(message)

    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(data.get("error") or f"API reported failure: {path}")

    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected API payload type from {path}: {type(data).__name__}")
    return data


def _safe_int(value: Any, field: str, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc


def _safe_float(value: Any, field: str, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc


def _safe_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


# ==============================================================================
# TOOLS
# ==============================================================================

@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available K6 MCP tools."""
    return [
        Tool(
            name="k6_verify_connection",
            description="Connect to K6 device and verify firmware version. Returns device status.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="k6_get_status",
            description="Get current K6 device status (connected, dry_run, mock_mode, version).",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="k6_load_image",
            description="Load image for burning. Can load sample image by name or provide base64 data. Default: default-image.png",
            inputSchema={
                "type": "object",
                "properties": {
                    "image_name": {
                        "type": "string",
                        "description": "Name of sample image (e.g. 'default-image.png') or 'custom' for base64"
                    },
                    "image_base64": {
                        "type": "string",
                        "description": "Base64 PNG data (if image_name='custom')"
                    }
                },
                "required": []
            }
        ),
        Tool(
            name="k6_generate_qr",
            description="Generate WiFi QR code for burning. Returns base64 image + metadata.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ssid": {
                        "type": "string",
                        "description": "WiFi network name (SSID)"
                    },
                    "password": {
                        "type": "string",
                        "description": "WiFi password"
                    },
                    "security": {
                        "type": "string",
                        "enum": ["WPA", "WEP", "nopass"],
                        "description": "WiFi security type (default: WPA)"
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional label text below QR code"
                    },
                    "show_password": {
                        "type": "boolean",
                        "description": "Show password in label (default: false)"
                    }
                },
                "required": ["ssid"]
            }
        ),
        Tool(
            name="k6_burn_job",
            description="Execute burn job. DRY_RUN=true by default (safe testing). Set dry_run=false to actually fire laser.",
            inputSchema={
                "type": "object",
                "properties": {
                    "image_base64": {
                        "type": "string",
                        "description": "Base64 PNG image data to burn"
                    },
                    "power": {
                        "type": "integer",
                        "description": "Laser power 0-1000 (default: 500)",
                        "minimum": 0,
                        "maximum": 1000
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Burn depth 1-255 (default: 10 - light test mark)",
                        "minimum": 1,
                        "maximum": 255
                    },
                    "center_x": {
                        "type": "integer",
                        "description": "X position in pixels (default: auto-center)"
                    },
                    "center_y": {
                        "type": "integer",
                        "description": "Y position in pixels (default: auto-center)"
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Dry run mode (default: true - no laser firing)"
                    }
                },
                "required": ["image_base64"]
            }
        ),
        Tool(
            name="k6_list_samples",
            description="List available sample images from /app/images directory.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        Tool(
            name="k6_set_dry_run",
            description="Enable/disable dry run mode. DRY_RUN=true prevents laser firing (safe testing).",
            inputSchema={
                "type": "object",
                "properties": {
                    "enabled": {
                        "type": "boolean",
                        "description": "Enable dry run mode (true=safe, false=LASER FIRES)"
                    }
                },
                "required": ["enabled"]
            }
        ),
        Tool(
            name="k6_raw_send",
            description="Send raw hex bytes directly to K6 via Flask lab endpoint and return raw response bytes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "packet_hex": {
                        "type": "string",
                        "description": "Hex bytes, e.g. 'ff000400' or 'ff 00 04 00'"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Read timeout in seconds (default: 2.0)"
                    },
                    "read_size": {
                        "type": "integer",
                        "description": "Maximum response bytes to read (default: 256)"
                    },
                    "flush_input": {
                        "type": "boolean",
                        "description": "Clear pending input bytes before send (default: false)"
                    },
                    "settle_ms": {
                        "type": "integer",
                        "description": "Idle settle window after response starts (default: 120)"
                    },
                    "save_logs": {
                        "type": "boolean",
                        "description": "Write byte dump logs in /app/data (default: true)"
                    }
                },
                "required": ["packet_hex"]
            }
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent | ImageContent | EmbeddedResource]:
    """Handle MCP tool calls."""
    arguments = arguments or {}
    
    if name == "k6_verify_connection":
        return await tool_verify_connection()
    
    elif name == "k6_get_status":
        return await tool_get_status()
    
    elif name == "k6_load_image":
        return await tool_load_image(arguments)
    
    elif name == "k6_generate_qr":
        return await tool_generate_qr(arguments)
    
    elif name == "k6_burn_job":
        return await tool_burn_job(arguments)
    
    elif name == "k6_list_samples":
        return await tool_list_samples()
    
    elif name == "k6_set_dry_run":
        return await tool_set_dry_run(arguments)

    elif name == "k6_raw_send":
        return await tool_raw_send(arguments)
    
    else:
        return _tool_error(f"Unknown tool: {name}")


# ==============================================================================
# TOOL IMPLEMENTATIONS - HTTP Client (calls Flask API)
# ==============================================================================

async def tool_verify_connection() -> list[TextContent]:
    """Connect to K6 and verify firmware via Flask API."""
    try:
        data = await _api_json("POST", "/api/connect", timeout=10.0)
        result = {
            "success": data.get("success", False),
            "connected": data.get("connected", False),
            "version": data.get("version", "unknown"),
            "message": "Connected via Flask API",
            "api_endpoint": "/api/connect",
        }
        return _tool_text(result)
    except (httpx.HTTPError, RuntimeError, ValueError) as e:
        logger.error(f"Verify connection failed: {e}")
        return _tool_error(str(e), hint="Is Flask API running on port 8080?")


async def tool_get_status() -> list[TextContent]:
    """Get current K6 device status via Flask API."""
    try:
        status = await _api_json("GET", "/api/status", timeout=5.0)
        status["api_endpoint"] = "/api/status"
        return _tool_text(status)
    except (httpx.HTTPError, RuntimeError, ValueError) as e:
        logger.error(f"Get status failed: {e}")
        return _tool_error(str(e))


async def tool_load_image(arguments: dict) -> list[TextContent]:
    """Load image (sample or custom base64) via Flask API."""
    try:
        image_name = arguments.get("image_name", "default-image.png")
        
        if image_name == "custom":
            # Custom base64 provided - can return directly
            image_base64 = arguments.get("image_base64", "")
            if not image_base64:
                return _tool_error("image_base64 required when image_name='custom'")
            
            result = {
                "success": True,
                "source": "custom",
                "image_base64": image_base64,
                "note": "Custom image ready for burn"
            }
        else:
            # Load sample image via Flask API
            client = await _get_api_client()
            img_response = await client.get(f"/api/images/serve/{image_name}", timeout=10.0)
            if not img_response.is_success:
                return _tool_error(f"Image not found: {image_name}")

            # Convert to base64
            img_bytes = img_response.content
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            data_url = f"data:image/png;base64,{b64}"

            # Get image info from PIL
            from PIL import Image
            img = Image.open(io.BytesIO(img_bytes))

            result = {
                "success": True,
                "source": "sample",
                "filename": image_name,
                "width": img.width,
                "height": img.height,
                "mode": img.mode,
                "image_base64": data_url,
                "api_endpoint": f"/api/images/serve/{image_name}",
            }
        
        return _tool_text(result)
    
    except (httpx.HTTPError, RuntimeError, ValueError, OSError) as e:
        logger.error(f"Load image failed: {e}")
        return _tool_error(str(e))


async def tool_generate_qr(arguments: dict) -> list[TextContent]:
    """Generate WiFi QR code via Flask API."""
    try:
        ssid = arguments.get("ssid", "")
        password = arguments.get("password", "")
        security = arguments.get("security", "WPA")
        description = arguments.get("description", "")
        show_password = arguments.get("show_password", False)
        
        if not ssid:
            return _tool_error("ssid required")

        result = await _api_json(
            "POST",
            "/api/qr/generate",
            json_body={
                "ssid": ssid,
                "password": password,
                "security": security,
                "description": description,
                "show_password": _safe_bool(show_password, False),
            },
            timeout=10.0,
        )
        result["api_endpoint"] = "/api/qr/generate"
        return _tool_text(result)
    
    except (httpx.HTTPError, RuntimeError, ValueError) as e:
        logger.error(f"Generate QR failed: {e}")
        return _tool_error(str(e))


async def tool_burn_job(arguments: dict) -> list[TextContent]:
    """Execute burn job via Flask API."""
    try:
        # Prepare burn request - Flask API handles temp file creation
        burn_request = {
            "temp_path": arguments.get("temp_path", ""),  # If provided
            "power": _safe_int(arguments.get("power"), "power", 500),
            "depth": _safe_int(arguments.get("depth"), "depth", 10),
            "center_x": arguments.get("center_x"),
            "center_y": arguments.get("center_y"),
            "dry_run": _safe_bool(arguments.get("dry_run"), True),
        }
        if burn_request["center_x"] is not None:
            burn_request["center_x"] = _safe_int(arguments.get("center_x"), "center_x", 0)
        if burn_request["center_y"] is not None:
            burn_request["center_y"] = _safe_int(arguments.get("center_y"), "center_y", 0)
        
        # If image_base64 provided, call prepare first
        image_base64 = arguments.get("image_base64", "")
        if image_base64 and not burn_request["temp_path"]:
            # Call /api/engrave/prepare to get temp_path
            if image_base64.startswith("data:image"):
                image_base64 = image_base64.split(",", 1)[1]
            img_bytes = base64.b64decode(image_base64)
            files = {"image": ("mcp_upload.png", io.BytesIO(img_bytes), "image/png")}
            prepare_data = await _api_json(
                "POST",
                "/api/engrave/prepare",
                files=files,
                timeout=30.0,
            )
            burn_request["temp_path"] = prepare_data.get("temp_path")
        
        if not burn_request["temp_path"]:
            return _tool_error("No image provided (need temp_path or image_base64)")
        
        # Execute burn via Flask API
        result = await _api_json(
            "POST",
            "/api/engrave",
            json_body=burn_request,
            timeout=300.0,
        )
        result["api_endpoint"] = "/api/engrave"
        result["via_mcp"] = True
        return _tool_text(result)
    
    except (httpx.HTTPError, RuntimeError, ValueError, OSError) as e:
        logger.error(f"Burn job failed: {e}")
        return _tool_error(str(e))


async def tool_list_samples() -> list[TextContent]:
    """List available sample images via Flask API."""
    try:
        result = await _api_json("GET", "/api/images/list", timeout=5.0)
        result["api_endpoint"] = "/api/images/list"
        return _tool_text(result)
    
    except (httpx.HTTPError, RuntimeError, ValueError) as e:
        logger.error(f"List samples failed: {e}")
        return _tool_error(str(e))


async def tool_set_dry_run(arguments: dict) -> list[TextContent]:
    """Set dry run mode via Flask API settings."""
    try:
        enabled = _safe_bool(arguments.get("enabled"), True)
        result = await _api_json(
            "POST",
            "/api/settings",
            json_body={"dry_run": enabled},
            timeout=5.0,
        )
        result["api_endpoint"] = "/api/settings"
        return _tool_text(result)
    
    except (httpx.HTTPError, RuntimeError, ValueError) as e:
        logger.error(f"Set dry run failed: {e}")
        return _tool_error(str(e))


async def tool_raw_send(arguments: dict) -> list[TextContent]:
    """Send raw protocol bytes via Flask lab endpoint."""
    try:
        packet_hex = str(arguments.get("packet_hex", "")).strip()
        if not packet_hex:
            return _tool_error("packet_hex required")

        payload = {
            "packet_hex": packet_hex,
            "timeout": _safe_float(arguments.get("timeout"), "timeout", 2.0),
            "read_size": _safe_int(arguments.get("read_size"), "read_size", 256),
            "flush_input": _safe_bool(arguments.get("flush_input"), False),
            "settle_ms": _safe_int(arguments.get("settle_ms"), "settle_ms", 120),
            "connect_if_needed": True,
            "save_logs": _safe_bool(arguments.get("save_logs"), True),
        }

        result = await _api_json(
            "POST",
            "/api/lab/raw/send",
            json_body=payload,
            timeout=35.0,
        )
        result["api_endpoint"] = "/api/lab/raw/send"
        result["via_mcp"] = True
        return _tool_text(result)

    except (httpx.HTTPError, RuntimeError, ValueError) as e:
        logger.error(f"Raw send failed: {e}")
        return _tool_error(str(e))


# ==============================================================================
# FASTMCP TRANSPORT WRAPPERS (NETWORK MODE)
# ==============================================================================

def _legacy_tool_result_to_payload(result: list[TextContent | ImageContent | EmbeddedResource]) -> Dict[str, Any]:
    """Convert legacy low-level tool output to plain JSON-serializable payload."""
    if not result:
        return {"success": False, "error": "Empty MCP tool response"}
    first = result[0]
    text = getattr(first, "text", None)
    if text is None:
        return {"success": True, "result": str(first)}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        return {"success": True, "result": parsed}
    except ValueError:
        return {"success": True, "result": text}


if mcp_http is not None:
    @mcp_http.tool()
    async def k6_verify_connection() -> Dict[str, Any]:
        return _legacy_tool_result_to_payload(await tool_verify_connection())


    @mcp_http.tool()
    async def k6_get_status() -> Dict[str, Any]:
        return _legacy_tool_result_to_payload(await tool_get_status())


    @mcp_http.tool()
    async def k6_load_image(image_name: str = "default-image.png", image_base64: str = "") -> Dict[str, Any]:
        return _legacy_tool_result_to_payload(
            await tool_load_image({"image_name": image_name, "image_base64": image_base64})
        )


    @mcp_http.tool()
    async def k6_generate_qr(
        ssid: str,
        password: str = "",
        security: str = "WPA",
        description: str = "",
        show_password: bool = False,
    ) -> Dict[str, Any]:
        return _legacy_tool_result_to_payload(
            await tool_generate_qr(
                {
                    "ssid": ssid,
                    "password": password,
                    "security": security,
                    "description": description,
                    "show_password": show_password,
                }
            )
        )


    @mcp_http.tool()
    async def k6_burn_job(
        image_base64: str = "",
        temp_path: str = "",
        power: int = 500,
        depth: int = 10,
        center_x: Optional[int] = None,
        center_y: Optional[int] = None,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        return _legacy_tool_result_to_payload(
            await tool_burn_job(
                {
                    "image_base64": image_base64,
                    "temp_path": temp_path,
                    "power": power,
                    "depth": depth,
                    "center_x": center_x,
                    "center_y": center_y,
                    "dry_run": dry_run,
                }
            )
        )


    @mcp_http.tool()
    async def k6_list_samples() -> Dict[str, Any]:
        return _legacy_tool_result_to_payload(await tool_list_samples())


    @mcp_http.tool()
    async def k6_set_dry_run(enabled: bool = True) -> Dict[str, Any]:
        return _legacy_tool_result_to_payload(await tool_set_dry_run({"enabled": enabled}))


    @mcp_http.tool()
    async def k6_raw_send(
        packet_hex: str,
        timeout: float = 2.0,
        read_size: int = 256,
        flush_input: bool = False,
        settle_ms: int = 120,
        save_logs: bool = True,
    ) -> Dict[str, Any]:
        return _legacy_tool_result_to_payload(
            await tool_raw_send(
                {
                    "packet_hex": packet_hex,
                    "timeout": timeout,
                    "read_size": read_size,
                    "flush_input": flush_input,
                    "settle_ms": settle_ms,
                    "save_logs": save_logs,
                }
            )
        )


# ==============================================================================
# MAIN - HTTP/SSE Server
# ==============================================================================

def main():
    """Run MCP server via network transport."""
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8081"))
    requested_transport = os.getenv("MCP_TRANSPORT", "streamable-http").lower()

    if mcp_http is not None:
        logger.info("Starting FastMCP network transport")
        logger.info(f"Transport request: {requested_transport}")
        logger.info(f"Listening on http://{host}:{port}")

        settings = getattr(mcp_http, "settings", None)
        if settings is not None:
            if hasattr(settings, "host"):
                settings.host = host
            if hasattr(settings, "port"):
                settings.port = port
            if hasattr(settings, "streamable_http_path"):
                settings.streamable_http_path = "/mcp"
            if hasattr(settings, "stateless_http"):
                settings.stateless_http = True

        transport = "streamable-http"
        if requested_transport in {"sse", "legacy-sse"}:
            transport = "sse"

        # SDK compatibility across versions: try richer signature first.
        try:
            mcp_http.run(
                transport=transport,
                host=host,
                port=port,
                stateless_http=True,
            )
            return
        except TypeError:
            try:
                mcp_http.run(transport=transport, host=host, port=port)
                return
            except TypeError:
                mcp_http.run(transport=transport)
                return

    logger.warning("FastMCP unavailable; falling back to low-level SDK SSE transport")
    from contextlib import asynccontextmanager
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    
    logger.info("Starting K6 MCP server on legacy HTTP/SSE transport...")
    logger.info(f"Listening on http://{host}:{port}")
    
    sse = SseServerTransport("/messages/")

    def _legacy_init_options() -> InitializationOptions:
        """Initialization options for low-level Server transport."""
        return InitializationOptions(
            server_name="k6-laser-mcp",
            server_version="1.0.0",
            capabilities=app.get_capabilities(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        )

    async def handle_sse(request: Request):
        """Handle SSE connect endpoint for low-level MCP server."""
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            # Newer SDK versions may expose a helper; fallback to explicit options.
            if hasattr(app, "create_initialization_options"):
                init_options = app.create_initialization_options()
            else:
                init_options = _legacy_init_options()
            await app.run(streams[0], streams[1], init_options)

    async def handle_messages(request: Request):
        """Handle client-to-server POST channel for low-level SSE transport."""
        await sse.handle_post_message(request.scope, request.receive, request._send)

    async def health(_request: Request):
        """Lightweight health endpoint for supervisor/debugging."""
        return JSONResponse(
            {
                "ok": True,
                "service": "k6-laser-mcp",
                "flask_api_base": FLASK_API_BASE,
            }
        )
    
    routes = [
        Route("/sse", endpoint=handle_sse),
        Route("/messages/", endpoint=handle_messages, methods=["POST"]),
        Route("/health", endpoint=health),
    ]

    @asynccontextmanager
    async def lifespan(_app):
        await _get_api_client()
        try:
            yield
        finally:
            await _close_api_client()

    starlette_app = Starlette(debug=False, routes=routes, lifespan=lifespan)
    
    # Run with uvicorn
    uvicorn.run(
        starlette_app,
        host=host,
        port=port,
        log_level="info"
    )


if __name__ == "__main__":
    main()
