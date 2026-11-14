#!/usr/bin/env python3
"""Match an uploaded recording to run logs and report processing timings."""

import argparse
import array
import base64
import datetime
import io
import json
import math
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

SAMPLE_RATE = 8000
PLAYBACK_SAMPLE_RATE = 24000
WINDOW_MS = 40
HOP_MS = 10
DTMF_ROWS = (697, 770, 852, 941)
DTMF_COLS = (1209, 1336, 1477)
DTMF_DIGITS = (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9"), ("*", "0", "#"))
PREAMBLE_HZ = (660, 880)
FRAME_SAMPLES = SAMPLE_RATE * WINDOW_MS // 1000
HANN_WINDOW = tuple(0.5 - 0.5 * math.cos(2.0 * math.pi * index / (FRAME_SAMPLES - 1)) for index in range(FRAME_SAMPLES))
GOERTZEL_COEFFICIENTS = {frequency:2.0 * math.cos(2.0 * math.pi * frequency / SAMPLE_RATE) for frequency in (*DTMF_ROWS, *DTMF_COLS, *PREAMBLE_HZ)}
MAX_EMBED_AUDIO_BYTES = 40 * 1024 * 1024
MAX_SYNC_SCAN_SECONDS = 120


def command(args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def probe(path):
    result = command([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,sample_rate,channels,channel_layout,duration",
        "-show_entries", "format=duration:format_tags=creation_time", "-of", "json", str(path),
    ])
    metadata = json.loads(result.stdout)
    stream = metadata.get("streams", [{}])[0]
    return {
        "codec": stream.get("codec_name"),
        "sample_rate": int(stream.get("sample_rate", 0)),
        "channels": stream.get("channels"),
        "duration_s": float(stream.get("duration") or metadata.get("format", {}).get("duration", 0)),
        "creation_time": metadata.get("format", {}).get("tags", {}).get("creation_time"),
    }


def decode(path):
    result = command([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0",
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-acodec", "pcm_s16le", "-f", "s16le", "pipe:1",
    ])
    samples = array.array("h")
    samples.frombytes(result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def wav_bytes(samples):
    pcm = array.array("h", samples)
    if sys.byteorder != "little":
        pcm.byteswap()
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(pcm.tobytes())
    return output.getvalue()


def encode_playback_mp3(path):
    result = command([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0",
        "-ac", "1", "-ar", str(PLAYBACK_SAMPLE_RATE), "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1",
    ])
    return result.stdout


def goertzel_power(frame, frequency):
    coefficient = GOERTZEL_COEFFICIENTS[frequency]
    q1 = q2 = 0.0
    length = len(frame)
    window_values = HANN_WINDOW if length == FRAME_SAMPLES else tuple(0.5 - 0.5 * math.cos(2.0 * math.pi * index / (length - 1)) for index in range(length))
    for sample, window in zip(frame, window_values):
        q0 = sample / 32768.0 * window + coefficient * q1 - q2
        q2, q1 = q1, q0
    return max(0.0, q1 * q1 + q2 * q2 - coefficient * q1 * q2) / (length * length)


def classify(frame):
    mean_square = sum(sample * sample for sample in frame) / (len(frame) * 32768.0 * 32768.0)
    if mean_square < 1e-6:
        return None

    low_powers = sorted(((goertzel_power(frame, frequency), frequency) for frequency in DTMF_ROWS), reverse=True)
    high_powers = sorted(((goertzel_power(frame, frequency), frequency) for frequency in DTMF_COLS), reverse=True)
    low, low_hz = low_powers[0]
    high, high_hz = high_powers[0]
    if low > mean_square * 0.02 and high > mean_square * 0.02 and low_powers[1][0] < low * 0.60 and high_powers[1][0] < high * 0.60:
        return DTMF_DIGITS[DTMF_ROWS.index(low_hz)][DTMF_COLS.index(high_hz)]

    preamble_powers = sorted(((goertzel_power(frame, frequency), frequency) for frequency in PREAMBLE_HZ), reverse=True)
    strongest, frequency = preamble_powers[0]
    if strongest > mean_square * 0.02 and preamble_powers[1][0] < strongest * 0.45:
        return f"TONE{frequency}"
    return None


def classify_audio(samples):
    window = FRAME_SAMPLES
    hop = SAMPLE_RATE * HOP_MS // 1000
    segments = []
    active = None
    scan_end = min(len(samples), SAMPLE_RATE * MAX_SYNC_SCAN_SECONDS)
    for index, start in enumerate(range(0, max(0, scan_end - window + 1), hop)):
        label = classify(samples[start:start + window])
        start_s = index * HOP_MS / 1000.0
        if label == active and label is not None:
            segments[-1]["end_s"] = start_s + WINDOW_MS / 1000.0
        else:
            if active is not None:
                segments[-1]["end_s"] = max(segments[-1]["end_s"], start_s)
            if label is not None:
                segment = {"symbol":label,"start_s":start_s,"end_s":start_s + WINDOW_MS / 1000.0}
                if segments and segments[-1]["symbol"] == label and start_s - segments[-1]["end_s"] <= 0.04:
                    segments[-1]["end_s"] = segment["end_s"]
                else:
                    segments.append(segment)
            active = label
        if len(cue_digit_segments(segments)) >= 6:
            return segments
    return segments


def find_cue_preamble(segments):
    symbols = [segment["symbol"] for segment in segments]
    for index in range(len(symbols) - 2):
        if symbols[index:index + 3] == ["TONE880", "TONE660", "TONE880"]:
            first, second, third = segments[index:index + 3]
            first_gap = second["start_s"] - first["end_s"]
            second_gap = third["start_s"] - second["end_s"]
            if -0.02 <= first_gap <= 0.12 and -0.02 <= second_gap <= 0.12:
                return segments[index:index + 3]
    return []


def cue_digit_segments(segments):
    digits = [segment for segment in segments if segment["symbol"].isdigit()]
    preamble = find_cue_preamble(segments)
    if preamble:
        digits = [segment for segment in digits if segment["start_s"] >= preamble[-1]["end_s"]]
    return digits[:6]


def find_cue_start(segments):
    preamble = find_cue_preamble(segments)
    if preamble:
        return preamble[0]["start_s"]
    digits = [segment for segment in segments if segment["symbol"].isdigit()]
    return digits[0]["start_s"] if digits else None


def load_run_log(path):
    if path is None:
        return []
    content = path.read_text(encoding="utf-8")
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return [json.loads(line) for line in content.splitlines() if line.strip()]
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return value.get("events", [value])
    return []


def align_run_log(events, attempt_id, recording_id, cue_start_s, cue_to_request_ms):
    marker = next((event for event in events if event.get("event") == "sync_marker" and ((attempt_id and event.get("attempt_id") == attempt_id) or (recording_id and event.get("recording_id") == recording_id))), None)
    if marker is None:
        return {"marker_found": False, "trace_found": False, "events": []}
    attempt_id = attempt_id or marker.get("attempt_id")
    run = next((event for event in events if event.get("event") == "ran" and (event.get("attempt") or {}).get("id") == attempt_id), None)
    outcome = (run or {}).get("outcome") or {}
    trace = outcome.get("receive_trace") or {}
    marker_audio_s = cue_start_s + marker.get("cue_to_request_ms", cue_to_request_ms) / 1000.0 if cue_start_s is not None else None
    aligned = []
    if marker_audio_s is not None:
        run_started = outcome.get("started")
        run_ended = outcome.get("ended")
        if run_started is not None:
            aligned.append({"audio_time_s": round(marker_audio_s, 3), "job_index": None, "kind": "nominal_request"})
        trace_start = trace.get("started_at_unix_ms")
        attempt = (run or {}).get("attempt") or {}
        context = " / ".join(str(attempt[key]) for key in ("template", "step", "goal", "review_id", "stage_name") if attempt.get(key) is not None)
        if trace_start is not None:
            for event in trace.get("events", []):
                if event.get("at_ms") is None:
                    continue
                byte = event.get("byte")
                raw = event.get("frame_bytes")
                if isinstance(byte, str):
                    byte = int(byte, 16)
                if isinstance(raw, str):
                    raw = [int(value, 16) for value in raw.split()]
                aligned.append({
                    "audio_time_s": round(marker_audio_s + (trace_start - marker.get("at_ms", 0)) / 1000.0 + event.get("at_ms", 0) / 1000.0, 3),
                    "job_index": event.get("job_index"),
                    "phase": event.get("phase"),
                    "kind": event.get("kind"),
                    "byte": byte,
                    "frame": event.get("frame"),
                    "frame_bytes": raw,
                    "quiet_ms": event.get("quiet_ms"),
                    "percent": event.get("percent"),
                    "metadata": " / ".join(value for value in (context, event.get("metadata")) if value),
                })
        if run_ended is not None:
            if outcome.get("elapsed_ms") is not None:
                elapsed_s = outcome["elapsed_ms"] / 1000.0
            elif run_started is not None:
                elapsed_s = run_ended - run_started
            else:
                elapsed_s = (run_ended * 1000 - marker.get("at_ms", 0)) / 1000.0
            aligned.append({"audio_time_s": round(marker_audio_s + elapsed_s, 3), "job_index": None, "kind": "estimated_run_end"})
    return {
        "marker_found": True,
        "recording_id": marker.get("recording_id"),
        "attempt_id": attempt_id,
        "marker_at_ms": marker.get("at_ms"),
        "cue_to_request_ms": marker.get("cue_to_request_ms"),
        "trace_found": bool(trace),
        "trace_started_at_unix_ms": trace.get("started_at_unix_ms"),
        "trace_truncated": trace.get("truncated"),
        "trace_bytes_captured": trace.get("bytes_captured"),
        "events": aligned,
        "software_outcome": outcome.get("saw"),
    }


def waveform_svg(samples, duration_s, segments, aligned_log):
    import html

    left, top, bottom = 52, 48, 150
    duration_s = max(float(duration_s), 0.001)
    target_points = min(24000, max(1200, int(duration_s * 8)))
    chunk_size = max(1, math.ceil(len(samples) / target_points))
    envelope = []
    for start in range(0, len(samples), chunk_size):
        chunk = samples[start:start + chunk_size]
        if chunk:
            envelope.append((min(chunk) / 32768.0, max(chunk) / 32768.0))
    peaks = sorted(max(abs(low),abs(high)) for low,high in envelope)
    reference = peaks[min(len(peaks)-1, int((len(peaks)-1)*0.95))] if peaks else 1.0
    base_gain = min(100.0, 0.85 / max(reference, 0.0001))
    plot_width = max(1000, min(1520, len(envelope) - 1))
    width, height = left + plot_width + 24, 218
    y = lambda amplitude: top + (1.0 - max(-1.0, min(1.0, amplitude))) * (bottom - top) / 2
    path = "".join(f"M{left + index * plot_width / max(1, len(envelope) - 1):.1f} {y(high * base_gain):.1f}V{y(low * base_gain):.1f}" for index,(low,high) in enumerate(envelope))
    lines = [
        f'<defs><clipPath id="wave-clip"><rect x="{left}" y="{top}" width="{plot_width}" height="{bottom-top}"/></clipPath></defs>',
        f'<line x1="{left}" y1="{(top + bottom) / 2}" x2="{width - 20}" y2="{(top + bottom) / 2}" stroke="#b8c2c9" stroke-width="1"/>',
        f'<g id="envelope-scale" clip-path="url(#wave-clip)" transform="translate(0 {(top+bottom)/2}) scale(1 1) translate(0 {-((top+bottom)/2)})"><path d="{path}" fill="none" stroke="#18232b" stroke-width="1"/></g>',
    ]
    tick_step = max(5, math.ceil(duration_s / 12 / 5) * 5)
    for second in range(0, int(duration_s) + 1, tick_step):
        x = left + second / duration_s * plot_width
        lines.append(f'<line x1="{x:.1f}" y1="{top - 8}" x2="{x:.1f}" y2="{bottom + 8}" stroke="#d7dee3" stroke-width="1"/>')
        lines.append(f'<text x="{x:.1f}" y="{bottom + 25}" text-anchor="middle" font-size="11" fill="#40515c">{second}s</text>')
    cue_start = find_cue_start(segments)
    if cue_start is not None:
        x = left + cue_start / duration_s * plot_width
        lines.append(f'<line x1="{x:.1f}" y1="{top - 16}" x2="{x:.1f}" y2="{bottom + 8}" stroke="#6e45a8" stroke-width="2"><title>Decoded sync cue at {cue_start:.2f} seconds</title></line>')
    event_names = {"nominal_request":"Nominal request", "estimated_run_end":"Run-end estimate", "job_start":"Job start", "rx_byte":"Received byte", "status_frame":"Status frame", "heartbeat_frame":"Heartbeat frame", "quiet_timeout":"90-second quiet timeout", "silence_assumed":"Silence-assumed completion", "reported_complete":"Device-reported completion", "receive_error":"Receive error", "stalled":"Stalled"}
    event_colors = {"nominal_request":"#167d89", "estimated_run_end":"#a33f5b", "job_start":"#514578", "rx_byte":"#667985", "status_frame":"#137b63", "heartbeat_frame":"#1764a0", "quiet_timeout":"#bd3039", "silence_assumed":"#bd3039", "reported_complete":"#23834a", "receive_error":"#bd3039", "stalled":"#d58b39"}
    for event in aligned_log.get("events", []):
        second = float(event.get("audio_time_s", 0))
        if not 0 <= second <= duration_s:
            continue
        kind = event.get("kind", "")
        x = left + second / duration_s * plot_width
        label = event_names.get(kind, kind.replace("_", " "))
        color = event_colors.get(kind, "#514578")
        raw = event.get("frame_bytes")
        packet = " ".join(f"{int(value) & 255:02X}" for value in raw) if isinstance(raw, list) else (f"RX {int(event['byte']) & 255:02X}" if event.get("byte") is not None else str(event.get("frame") or ""))
        detail = f"{label} {packet}".strip()
        opacity = "0.35" if kind == "rx_byte" else "0.82"
        stroke_width = "1" if kind == "rx_byte" else "2"
        lines.append(f'<line x1="{x:.1f}" y1="{top - 8}" x2="{x:.1f}" y2="{bottom + 8}" stroke="{color}" stroke-width="{stroke_width}" opacity="{opacity}" stroke-dasharray="4 3"><title>{html.escape(detail)} at {second:.2f} seconds</title></line>')
    lines.append(f'<rect x="{left}" y="{top}" width="{plot_width}" height="{bottom - top}" fill="none" stroke="#667985"/>')
    lines.append(f'<line id="playhead" x1="{left}" y1="{top - 8}" x2="{left}" y2="{bottom + 8}" stroke="#d22f56" stroke-width="3" visibility="hidden" pointer-events="none"><title>Audio playback position</title></line>')
    return f'<svg id="timeline" xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" preserveAspectRatio="none" data-base-width="{width}" data-plot-left="{left}" data-plot-width="{plot_width}" role="slider" tabindex="0" aria-label="Auto-normalized min/max audio amplitude waveform aligned with run log; click to seek" aria-valuemin="0" aria-valuemax="{duration_s:.3f}" aria-valuenow="0">{"".join(lines)}</svg>'


def waveform_data(samples):
    bin_size = max(1, SAMPLE_RATE // 500)
    encoded = array.array("h")
    histogram = [0] * 32769
    for start in range(0, len(samples), bin_size):
        chunk = samples[start:start + bin_size]
        if not chunk:
            continue
        low, high = min(chunk), max(chunk)
        encoded.extend((low, high))
        histogram[max(abs(low), abs(high))] += 1
    target = max(1, math.ceil(sum(histogram) * 0.95))
    cumulative = 0
    reference = 1
    for amplitude, count in enumerate(histogram):
        cumulative += count
        if cumulative >= target:
            reference = max(1, amplitude)
            break
    if sys.byteorder != "little":
        encoded.byteswap()
    return {"minmax_base64":base64.b64encode(encoded.tobytes()).decode("ascii"),"reference":reference,"bin_ms":2}


def sync_timeline_events(marker):
    expected = marker.get("expected_id") or "unknown"
    decoded = marker.get("decoded_id")
    segments = []
    for kind, key in (("sync_tone", "preamble_tones"), ("sync_digit", "digit_tones")):
        for segment in marker.get(key, []):
            symbol = str(segment["symbol"])
            label = f"{symbol.removeprefix('TONE')} Hz" if kind == "sync_tone" else symbol
            segments.append({
                "kind": kind,
                "phase": "sync_audio",
                "audio_time_s": segment["start_s"],
                "end_s": segment["end_s"],
                "label": label,
                "metadata": f"Decoded from audio: {'preamble tone' if kind == 'sync_tone' else 'DTMF digit'} {label}",
            })
    cue_start = marker.get("cue_start_s")
    if cue_start is None and not segments:
        return []
    status = "not decoded" if decoded is None else ("match" if marker.get("matches_expected") is True else "mismatch" if marker.get("matches_expected") is False else "not compared")
    summary = {
        "kind": "sync_cue",
        "phase": "sync_audio",
        "audio_time_s": cue_start if cue_start is not None else min(segment["audio_time_s"] for segment in segments),
        "label": f"ID {decoded}" if decoded is not None else "ID not decoded",
        "metadata": f"Sync audio; expected ID {expected}; decoded ID {decoded if decoded is not None else 'not decoded'} ({status})",
    }
    if segments:
        summary["end_s"] = max(segment["end_s"] for segment in segments)
    return [summary, *segments]


def write_run_log_report(path, events, attempt_id):
    run = next(event for event in events if event.get("event") == "ran" and (event.get("attempt") or {}).get("id") == attempt_id)
    attempt = run["attempt"]
    outcome = run.get("outcome") or {}
    trace = outcome.get("receive_trace") or {}
    traced = trace.get("started_at_unix_ms") is not None
    basis = "Time since trace start" if traced else "Time since run start; no packet trace available"
    context = " / ".join(str(attempt[key]) for key in ("template", "step", "goal", "review_id", "stage_name") if attempt.get(key) is not None)
    aligned = []
    for event in trace.get("events", []) if traced else []:
        if event.get("at_ms") is None:
            continue
        item = dict(event)
        item["audio_time_s"] = event["at_ms"] / 1000.0
        if isinstance(item.get("byte"), str):
            item["byte"] = int(item["byte"], 16)
        if isinstance(item.get("frame_bytes"), str):
            item["frame_bytes"] = [int(value, 16) for value in item["frame_bytes"].split()]
        item["metadata"] = " / ".join(value for value in (context, event.get("metadata")) if value)
        aligned.append(item)
    elapsed_ms = outcome.get("elapsed_ms", max(0, outcome.get("ended", 0) - outcome.get("started", 0)) * 1000)
    duration = max(0.001, (trace.get("duration_ms", 0) if traced else elapsed_ms) / 1000.0, max((event["audio_time_s"] for event in aligned), default=0))
    aligned.extend([
        {"kind":"trace_start" if traced else "run_start", "audio_time_s":0, "metadata":basis},
        {"kind":"trace_end" if traced else "estimated_run_end", "audio_time_s":duration, "metadata":str(outcome.get("saw") or "Software execution finished")},
    ])
    run_timings = {"elapsed_ms":elapsed_ms}
    for index, band in enumerate(outcome.get("band_timings") or [], 1):
        for key in ("transfer_ms", "setup_ms", "wait_ms", "elapsed_ms"):
            if band.get(key) is not None:
                run_timings[f"band_{index}_{key}"] = band[key]
    report = {"audio":{"available":False,"duration_s":duration}, "sync_marker":{"attempt_id":attempt_id}, "run_timings":run_timings, "interpretation_limit":f"{basis}. Only recorded events and timings are shown; no audio or motion state is inferred."}
    alignment = {"events":aligned,"summary":f"{context}. {basis}. {outcome.get('saw') or ''}","trace_truncated":trace.get("truncated",False)}
    write_html(path, b"", "", "Run analysis", report, alignment, {"minmax_base64":"","reference":1,"bin_ms":2})


def write_html(path, playback_audio, playback_name, audio_name, report, aligned_log, waveform, timing=None):
    import html

    duration_s = report["audio"]["duration_s"]
    has_audio = report["audio"].get("available", True)
    title = f"Audio run analysis: {audio_name}" if has_audio else "Run analysis"
    marker = report["sync_marker"]
    expected_id = marker.get("expected_id") or "unknown"
    decoded_id = marker.get("decoded_id") or "not decoded"
    match = marker.get("matches_expected")
    match_label = "not decoded" if marker.get("decoded_id") is None else ("match" if match else ("mismatch" if match is False else "not compared"))
    event_names = {"nominal_request": "Nominal run request", "estimated_run_end": "Estimated run end", "job_start": "Job start", "rx_byte": "Received byte", "status_frame": "Status frame", "heartbeat_frame": "Heartbeat frame", "quiet_timeout": "90-second quiet timeout", "silence_assumed": "Silence-assumed completion", "reported_complete": "Device-reported completion", "receive_error": "Receive error", "stalled": "Stalled"}
    def format_log_row(index, event):
        raw = event.get("frame_bytes")
        packet = " ".join(f"{int(value) & 255:02X}" for value in raw) if isinstance(raw, list) else (f"RX {int(event['byte']) & 255:02X}" if event.get("byte") is not None else (event.get("frame") or ""))
        if event.get("quiet_ms") is not None:
            packet = f"{packet} · quiet {event['quiet_ms']} ms" if packet else f"quiet {event['quiet_ms']} ms"
        cells = (
            str(event.get("job_index") or ""),
            str(event.get("phase") or ""),
            event_names.get(event.get("kind"), str(event.get("kind") or "").replace("_", " ")),
            packet or str(event.get("metadata") or ""),
            f"{float(event.get('audio_time_s', 0)):.3f}",
            str(event.get("percent") if event.get("percent") is not None else ""),
        )
        return f'<tr class="log-event" data-event-index="{index}" data-audio-time="{float(event.get("audio_time_s", 0)):.3f}" tabindex="0">' + "".join(f"<td>{html.escape(value)}</td>" for value in cells) + "</tr>"
    timeline_events = list(aligned_log.get("events", []))
    timeline_events.extend(sync_timeline_events(marker))
    timeline_events.sort(key=lambda event: event["audio_time_s"])
    log_rows = "".join(format_log_row(index, event) for index, event in enumerate(timeline_events))
    timeline_data = json.dumps({"duration":duration_s,"has_audio":has_audio,"events":timeline_events,"waveform":waveform}, separators=(",", ":")).replace("<", "\\u003c")
    timeline_script = Path(__file__).with_name("timeline.js").read_text(encoding="utf-8")
    timing_rows = "".join(f"<tr><th>{html.escape(name.replace('_', ' ').title())}</th><td>{value} ms</td></tr>" for name, value in (timing or {}).items())
    if aligned_log.get("marker_found"):
        trace_summary = "Receive trace present." if aligned_log.get("trace_found") else "No receive trace was recorded, so per-job and quiet-timeout markers are unavailable."
        log_summary = f'Matched attempt {html.escape(str(aligned_log.get("attempt_id")))} to recording ID {html.escape(str(aligned_log.get("recording_id")))}. Outcome: {html.escape(str(aligned_log.get("software_outcome") or "not recorded"))}. {trace_summary} API start/end timestamps have one-second resolution; the end marker is duration-aligned from the nominal request.'
        if aligned_log.get("trace_truncated"):
            log_summary += " Trace truncated: some packets are missing."
    else:
        log_summary = "No matching sync marker found in the supplied run log."
    if not has_audio:
        log_summary = html.escape(aligned_log["summary"])
        if aligned_log.get("trace_truncated"):
            log_summary += " Trace truncated: some packets are missing."
    playback_type = "audio/mpeg" if playback_name.lower().endswith(".mp3") else "audio/wav"
    if len(playback_audio) <= MAX_EMBED_AUDIO_BYTES:
        encoded_audio = base64.b64encode(playback_audio).decode("ascii")
        audio_source = f"data:{playback_type};base64,{encoded_audio}"
        audio_picker = ""
    else:
        audio_source = ""
        sidecar_path = path.with_name(f"{path.stem}{Path(playback_name).suffix}")
        sidecar_path.write_bytes(playback_audio)
        audio_picker = f'<p><label>Choose {html.escape(sidecar_path.name)} to play this larger recording: <input id="audio-file" type="file" accept="{playback_type},.{Path(playback_name).suffix.lstrip('.')}"/></label></p>'
    audio_src = f' src="{audio_source}"' if audio_source else ""
    timeline_heading = "Recording waveform and run-log alignment" if has_audio else "Run event timeline"
    report_note = "The recording is provided for operator playback. This report presents sync matching, run-log events and measured timings only; it does not infer motion or laser state from sound." if has_audio else "This report presents recorded run events and measured timings only; no recording was supplied."
    playback = f'<audio controls preload="metadata"{audio_src}>Audio playback is not supported by this browser.</audio>' if has_audio else '<p class="meta">No audio recording. Run events and timings remain available.</p>'
    run_timing_rows = "".join(f"<tr><th>{html.escape(name.replace('_', ' ').title())}</th><td>{value} ms</td></tr>" for name, value in report.get("run_timings", {}).items())
    document = f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font:15px 'DejaVu Sans',sans-serif;max-width:1600px;margin:24px auto;padding:0 18px;color:#18232b;background:#f4f6f7}}
h1{{font-size:24px}} .meta{{color:#40515c}} .pill{{display:inline-block;padding:4px 8px;border:1px solid #71808a;border-radius:4px;background:#fff}}
audio{{width:100%;margin:12px 0}} .timeline-controls{{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:center}} .timeline-controls label{{margin:0;display:flex;gap:8px;align-items:center;flex-wrap:wrap}} input[type=number]{{width:5em}} input[type=range]{{max-width:100%}} .timeline-scroll{{max-width:100%;overflow-x:auto;background:#fff;border:1px solid #aeb8bf}} #waveform,#overview{{display:block;width:100%;background:#fff}} #waveform{{height:440px;cursor:crosshair}} #overview{{height:76px;cursor:grab;touch-action:none}} .timeline-readout{{display:flex;flex-wrap:wrap;gap:16px;align-items:center;margin:10px 0;font-variant-numeric:tabular-nums}} #event-detail,#timeline-tooltip{{overflow-wrap:anywhere;min-height:1.5em}} #event-detail{{padding:10px 0;border-bottom:1px solid #aeb8bf}} #timeline-tooltip{{color:#40515c}} .log-event{{cursor:pointer}} .log-event:hover,.log-event[aria-selected=true]{{background:#e8eef3}} .log-summary{{margin:12px 0;padding:9px 12px;border-left:3px solid #167d89;background:#fff;color:#18232b}}
section{{margin-top:24px}} table{{border-collapse:collapse;background:#fff}} th,td{{padding:6px 12px;border:1px solid #c5cdd2;text-align:left}} code{{font-family:ui-monospace,monospace}}
.sync-legend{{color:#6d4ba1}} .sync-legend::before{{content:"";display:inline-block;width:12px;height:12px;margin-right:6px;background:#e8def3;border:1px solid #6d4ba1}}
</style>
<h1>{'Audio run analysis' if has_audio else 'Run analysis'}</h1>
<p class="meta">{html.escape(audio_name)} · {duration_s:.2f} s</p>
<p>{f'<span class="pill">Expected ID: {html.escape(expected_id)}</span> <span class="pill">Decoded ID: {html.escape(decoded_id)} ({match_label})</span>' if has_audio else ''} <span class="pill">Attempt: {html.escape(str(marker.get("attempt_id") or "not supplied"))}</span></p>
{playback}
{audio_picker}
{'<p class="sync-legend">TX: sync audio / decoded ID</p>' if has_audio else ''}
<section><h2>{timeline_heading}</h2><div class="timeline-controls"><label>Time zoom <input id="time-zoom" type="range" min="1" max="128" step="0.01" value="1"><output id="time-zoom-value">1x</output></label><label>Amplitude <input id="amplitude-zoom" type="number" min="0.25" max="32" step="0.25" value="1">x</label><label>Reference <select id="amplitude-reference"><option value="peak">95th-percentile peak</option><option value="full">Full scale</option></select></label><button id="timeline-end" type="button">Jump to end</button></div><div class="timeline-readout"><output id="window-time"></output><span>Cursor <output id="cursor-time">00:00.000</output></span></div><canvas id="overview" width="1600" height="76" tabindex="0" aria-label="Full timeline; drag visible window or its edges, scroll, or use arrow keys to pan"></canvas><canvas id="waveform" width="1600" height="440" aria-label="Timeline with aligned Laser RX, Host TX, Jobs / bands and Run / sync lanes"></canvas><p id="timeline-tooltip" hidden></p><div id="event-detail" role="status">No event selected</div><button id="seek-selection" type="button" disabled>Seek to selection</button></section>
<p class="meta">{report_note}</p>
<p class="log-summary">{log_summary}</p>
{f'<section><h2>Run timings</h2><table><tbody>{run_timing_rows}</tbody></table></section>' if run_timing_rows else ''}
<section><h2>Run-log alignment</h2><div class="timeline-scroll"><table><thead><tr><th>Job</th><th>Phase</th><th>Event</th><th>Received packet / byte</th><th>{'Audio time' if has_audio else 'Relative time'} (s)</th><th>Progress</th></tr></thead><tbody>{log_rows}</tbody></table></div></section>
{f'<section><h2>Analyzer processing time</h2><table><thead><tr><th>Phase</th><th>Time</th></tr></thead><tbody>{timing_rows}</tbody></table><p class="meta">These analyzer-side durations exclude recording time and the Pi-to-analyzer network transfer.</p></section>' if timing_rows else ''}
<p class="meta">{html.escape(report["interpretation_limit"])}</p>
<footer>Copyright (c) 2026 Sean Donnellan</footer>
<script>
const timelineData={timeline_data};
{timeline_script}
</script></html>'''
    path.write_text(document, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--attempt-id", help="planned attempt ID, for example 1794321995-frame")
    parser.add_argument("--expected-id", help="expected six-digit ID, if known from the run log")
    parser.add_argument("--run-log", type=Path, help="API /api/log JSON array or runs.jsonl export")
    parser.add_argument("--html-report", type=Path, help="HTML report output path (default: recording.analysis.html)")
    parser.add_argument("--marker-at-ms", type=int, help="sync_marker at_ms from the run log")
    parser.add_argument("--cue-to-request-ms", type=int, default=2700, help="logged cue-to-run-request delay")
    args = parser.parse_args()
    if not args.recording.is_file():
        parser.error(f"recording not found: {args.recording}")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        parser.error("ffmpeg and ffprobe must be present in the analysis container")

    phase_started = time.perf_counter_ns()
    metadata = probe(args.recording)
    probe_ms = (time.perf_counter_ns() - phase_started) // 1_000_000
    phase_started = time.perf_counter_ns()
    samples = decode(args.recording)
    decode_ms = (time.perf_counter_ns() - phase_started) // 1_000_000
    phase_started = time.perf_counter_ns()
    segments = classify_audio(samples)
    digits = cue_digit_segments(segments)
    decoded_id = "".join(segment["symbol"] for segment in digits[:6]) if len(digits) >= 6 else None
    preamble = find_cue_preamble(segments)
    cue_start = find_cue_start(segments)
    run_log = load_run_log(args.run_log)

    expected_id = args.expected_id
    if expected_id is None and args.attempt_id:
        timestamp = args.attempt_id.split("-")[0]
        if timestamp.isdigit() and len(timestamp) >= 6:
            expected_id = timestamp[-6:]
    creation_time = metadata.get("creation_time")
    creation_epoch_ms = None
    if creation_time:
        try:
            creation_epoch_ms = int(datetime.datetime.fromisoformat(creation_time.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            pass
    run_request_s = round(cue_start + args.cue_to_request_ms / 1000.0, 3) if cue_start is not None else None
    alignment = align_run_log(run_log, args.attempt_id, decoded_id or expected_id, cue_start, args.cue_to_request_ms)
    sync_correlation_ms = (time.perf_counter_ns() - phase_started) // 1_000_000
    if args.marker_at_ms is not None:
        alignment["marker_at_ms"] = args.marker_at_ms
        alignment["cue_to_request_ms"] = args.cue_to_request_ms
    report = {
        "recording": args.recording.name,
        "audio": metadata,
        "playback_audio": {"codec": "mp3", "bitrate": 64000, "sample_rate": PLAYBACK_SAMPLE_RATE, "channels": 1},
        "tools": {"decoder": "FFmpeg mono 8 kHz PCM", "dtmf": "Python standard-library Goertzel", "ffmpeg_version": command(["ffmpeg", "-version"]).stdout.decode().splitlines()[0]},
        "sync_marker": {
            "expected_id": expected_id,
            "decoded_id": decoded_id,
            "matches_expected": decoded_id == expected_id if decoded_id is not None and expected_id else None,
            "preamble_tones": preamble[:3],
            "digit_tones": digits[:6],
            "cue_start_s": cue_start,
            "cue_to_request_ms": args.cue_to_request_ms,
            "nominal_run_request_s": run_request_s,
            "attempt_id": args.attempt_id,
            "marker_at_ms": args.marker_at_ms,
            "marker_minus_file_creation_s": round(args.marker_at_ms / 1000.0 - creation_epoch_ms / 1000.0, 3) if args.marker_at_ms is not None and creation_epoch_ms is not None else None,
            "sync_scan_limit_s": MAX_SYNC_SCAN_SECONDS,
        },
        "run_log_alignment": alignment,
        "interpretation_limit": f"The recording is provided for playback with a full-length waveform. DTMF sync matching scans only the first {MAX_SYNC_SCAN_SECONDS} seconds; the report aligns run-log events and measured timings but does not infer motion or laser state from sound.",
    }
    report_path = args.html_report or args.recording.with_name(args.recording.stem + ".analysis.html")
    playback_name = args.recording.stem + ".mp3"
    phase_started = time.perf_counter_ns()
    playback_audio = encode_playback_mp3(args.recording)
    playback_encode_ms = (time.perf_counter_ns() - phase_started) // 1_000_000
    timing = {"probe_ms":probe_ms,"decode_ms":decode_ms,"sync_correlation_ms":sync_correlation_ms,"playback_encode_ms":playback_encode_ms}
    timing["analysis_compute_ms"] = sum(timing.values())
    report["timing"] = timing
    phase_started = time.perf_counter_ns()
    waveform = waveform_data(samples)
    write_html(report_path, playback_audio, playback_name, args.recording.name, report, alignment, waveform, timing)
    timing["report_write_ms"] = (time.perf_counter_ns() - phase_started) // 1_000_000
    timing["analyzer_total_ms"] = timing["analysis_compute_ms"] + timing["report_write_ms"]
    report["timing"] = timing
    report["html_report"] = report_path.name
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()