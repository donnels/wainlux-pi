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
MAX_TRACE_WINDOW = 100
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


def valid_attempt_id(attempt_id: str) -> bool:
    return bool(attempt_id) and all(character.isascii() and (character.isalnum() or character in "-_") for character in attempt_id)


def updates_since(events: list[dict[str, Any]], cursor: int, limit: int) -> dict[str, Any]:
    if cursor < 0:
        raise ValueError("cursor must be non-negative")
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    start = min(cursor, len(events))
    end = min(start + limit, len(events))
    return {"events": events[start:end], "next_cursor": end, "has_more": end < len(events)}


def audio_analysis_for_attempt(events: list[dict[str, Any]], attempt_id: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event") == "audio_analysis" and event.get("attempt_id") == attempt_id:
            return event
    return None


def short_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def job_entries(events: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    """Summarize completed attempts without including their raw trace events."""
    if limit < 1 or limit > 50:
        raise ValueError("limit must be between 1 and 50")

    verdicts = {
        event.get("id"): event
        for event in events
        if event.get("event") == "verdict" and event.get("id")
    }
    jobs = []
    for event in events:
        if event.get("event") != "ran":
            continue
        attempt = event.get("attempt") or {}
        outcome = event.get("outcome") or {}
        trace = outcome.get("receive_trace") or {}
        attempt_id = attempt.get("id")
        verdict = verdicts.get(attempt_id)
        progress = [
            trace_event.get("percent")
            for trace_event in trace.get("events", [])
            if trace_event.get("kind") == "status_frame"
            and trace_event.get("percent") is not None
        ]
        jobs.append({
            "attempt_id": attempt_id,
            "started": outcome.get("started"),
            "ended": outcome.get("ended"),
            "step": attempt.get("step"),
            "goal": short_text(attempt.get("goal"), 180),
            "stage": attempt.get("stage_name"),
            "ok": outcome.get("ok"),
            "saw": short_text(outcome.get("saw"), 180),
            "last_progress": progress[-1] if progress else None,
            "completion": completion_kind(outcome.get("saw", "")),
            "trace_bytes_captured": trace.get("bytes_captured", 0),
            "trace_truncated": trace.get("truncated", False),
            "verdict": None if verdict is None else {
                "status": verdict.get("status"),
                "ok": verdict.get("ok"),
                "at": verdict.get("at"),
                "comment": short_text(verdict.get("comment"), 120),
            },
        })
    return jobs[-limit:]


def completion_kind(saw: str) -> str:
    if "assumed complete" in saw or "assumed completion" in saw:
        return "silence_assumed"
    if "reported complete" in saw:
        return "reported_complete"
    if "cancelled" in saw:
        return "cancelled"
    if saw:
        return "returned_or_failed"
    return "unknown"


def find_job(events: list[dict[str, Any]], attempt_id: str) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("event") == "ran" and (event.get("attempt") or {}).get("id") == attempt_id:
            return event
    raise ValueError(f"no completed job found for attempt_id {attempt_id}")


def trace_window(events: list[dict[str, Any]], attempt_id: str, count: int, side: str) -> dict[str, Any]:
    if count < 1 or count > MAX_TRACE_WINDOW:
        raise ValueError(f"count must be between 1 and {MAX_TRACE_WINDOW}")
    event = find_job(events, attempt_id)
    outcome = event.get("outcome") or {}
    trace = outcome.get("receive_trace") or {}
    trace_events = trace.get("events") or []
    selected = trace_events[:count] if side == "head" else trace_events[-count:]
    return {
        "attempt_id": attempt_id,
        "side": side,
        "event_count": len(trace_events),
        "returned_events": len(selected),
        "bytes_captured": trace.get("bytes_captured", 0),
        "truncated": trace.get("truncated", False),
        "duration_ms": trace.get("duration_ms"),
        "events": selected,
    }


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
def get_updates(cursor: int = 0, limit: int = 100) -> dict[str, Any]:
    """Poll new redacted run-log events since a prior cursor and return current state.

    Pass the returned next_cursor on the next call. This is read-only and has no
    plan, run, stop, review, or verdict operation.
    """
    if cursor < 0:
        raise ValueError("cursor must be non-negative")
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    page = api_request(f"/api/log?cursor={cursor}&limit={limit}")
    page["state"] = api_request("/api/state")
    return page


@mcp.tool()
def get_job_toc(limit: int = 10) -> dict[str, Any]:
    """List recent completed jobs as compact summaries, newest last.

    Includes attempt IDs for trace lookup, timing, short goal/outcome, final
    progress, completion method, trace truncation, and compact verdict. Omits
    raw trace events and submitted content. limit is bounded to 1..50 jobs.
    """
    if limit < 1 or limit > 50:
        raise ValueError("limit must be between 1 and 50")
    result = api_request(f"/api/jobs/toc?limit={limit}")
    return result


@mcp.tool()
def get_job_trace_head(attempt_id: str, count: int = 30) -> dict[str, Any]:
    """Return the first trace events for one completed job.

    Use the attempt_id from get_job_toc. Includes trace size and truncation
    metadata. Opt-in raster bytes can reveal the image or QR data. count is
    bounded to 1..100 events.
    """
    if not valid_attempt_id(attempt_id):
        raise ValueError("invalid attempt ID")
    if count < 1 or count > MAX_TRACE_WINDOW:
        raise ValueError(f"count must be between 1 and {MAX_TRACE_WINDOW}")
    return api_request(f"/api/jobs/{attempt_id}/trace/head?count={count}")


@mcp.tool()
def get_job_trace_tail(attempt_id: str, count: int = 30) -> dict[str, Any]:
    """Return the last trace events for one completed job.

    Use the attempt_id from get_job_toc. Includes trace size and truncation
    metadata. Opt-in raster bytes can reveal the image or QR data. count is
    bounded to 1..100 events.
    """
    if not valid_attempt_id(attempt_id):
        raise ValueError("invalid attempt ID")
    if count < 1 or count > MAX_TRACE_WINDOW:
        raise ValueError(f"count must be between 1 and {MAX_TRACE_WINDOW}")
    return api_request(f"/api/jobs/{attempt_id}/trace/tail?count={count}")


@mcp.tool()
def get_audio_analysis(attempt_id: str) -> dict[str, Any]:
    """Return analyzer availability and timing/report metadata for an uploaded attempt.

    The result contains no recording bytes and does not fetch report HTML.
    """
    allowed = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_"
    if not attempt_id or any(character not in allowed for character in attempt_id):
        raise ValueError("invalid attempt ID")
    availability = api_request("/api/audio-analyses/availability")
    analysis = api_request(f"/api/audio-analyses/{attempt_id}/metadata").get("analysis")
    return {
        "attempt_id": attempt_id,
        "analyzer_available": bool(availability.get("available")),
        "analysis": None if not analysis else analysis,
    }


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
