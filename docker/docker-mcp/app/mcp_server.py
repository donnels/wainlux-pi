"""Draft, preview-only MCP client for the Rust K6 API."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mcp.server.fastmcp import FastMCP

API_BASE = os.environ.get("LASER_API_BASE_URL", "http://127.0.0.1:8081").rstrip("/")
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8082"))
MAX_REQUEST_BYTES = 256_000
mcp = FastMCP("wainlux-k6-preview", host=MCP_HOST, port=MCP_PORT)


def api_request(path: str, payload: dict[str, Any] | None = None) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{API_BASE}{path}",
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"Laser API returned HTTP {error.code} for {path}") from None
    except URLError as error:
        raise RuntimeError(f"Laser API unavailable for {path}: {error.reason}") from None
    except (TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Invalid or timed-out Laser API response for {path}") from error


@mcp.tool()
def get_machine() -> dict[str, Any]:
    """Return the connected service's machine profile and accepted settings."""
    return api_request("/api/machine")


@mcp.tool()
def list_materials() -> list[dict[str, Any]]:
    """List configured piece/backing materials and their machine settings."""
    return api_request("/api/materials")


@mcp.tool()
def list_templates() -> list[dict[str, Any]]:
    """List declared card templates and their static field/element policies."""
    return api_request("/api/templates")


@mcp.tool()
def preview_job(job: dict[str, Any]) -> dict[str, Any]:
    """Validate and compose a job without saving a review or contacting the laser.

    Returns stage names, sides, and raster geometry only. It omits generated
    pixels and submitted content. Use the operator UI to inspect exact pixels.
    """
    encoded_size = len(json.dumps(job).encode("utf-8"))
    if encoded_size > MAX_REQUEST_BYTES:
        raise ValueError(f"Job exceeds the {MAX_REQUEST_BYTES}-byte preview limit")

    preview = api_request("/api/jobs/preview", {"job": job})
    stages = []
    for stage in preview.get("stages", []):
        burns = [
            {
                key: burn[key]
                for key in ("x_mm", "y_mm", "w_mm", "h_mm", "width_px", "height_px")
                if key in burn
            }
            for burn in stage.get("burns", [])
        ]
        stages.append({"name": stage.get("name"), "side": stage.get("side"), "burns": burns})

    return {"layout": preview.get("layout"), "stages": stages, "preview_only": True}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
