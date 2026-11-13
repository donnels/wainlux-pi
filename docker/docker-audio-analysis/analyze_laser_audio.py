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
MAX_EMBED_AUDIO_BYTES = 40 * 1024 * 1024


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
    coefficient = 2.0 * math.cos(2.0 * math.pi * frequency / SAMPLE_RATE)
    q1 = q2 = 0.0
    length = len(frame)
    for index, sample in enumerate(frame):
        window = 0.5 - 0.5 * math.cos(2.0 * math.pi * index / (length - 1))
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
    window = SAMPLE_RATE * WINDOW_MS // 1000
    hop = SAMPLE_RATE * HOP_MS // 1000
    labels = []
    for start in range(0, len(samples) - window + 1, hop):
        frame = samples[start:start + window]
        labels.append(classify(frame))

    segments = []
    active = None
    for index, label in enumerate(labels + [None]):
        start_s = index * HOP_MS / 1000.0
        if label == active and label is not None:
            segments[-1]["end_s"] = start_s + WINDOW_MS / 1000.0
        else:
            if active is not None:
                segments[-1]["end_s"] = max(segments[-1]["end_s"], start_s)
            if label is not None:
                segments.append({"symbol": label, "start_s": start_s, "end_s": start_s + WINDOW_MS / 1000.0})
            active = label
    merged = []
    for segment in segments:
        if merged and segment["symbol"] == merged[-1]["symbol"] and segment["start_s"] - merged[-1]["end_s"] <= 0.04:
            merged[-1]["end_s"] = max(merged[-1]["end_s"], segment["end_s"])
        else:
            merged.append(segment)
    return merged


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
        if trace_start is not None:
            for event in trace.get("events", []):
                if event.get("kind") not in ("job_start", "quiet_timeout", "silence_assumed", "reported_complete"):
                    continue
                aligned.append({
                    "audio_time_s": round(marker_audio_s + (trace_start - marker.get("at_ms", 0)) / 1000.0 + event.get("at_ms", 0) / 1000.0, 3),
                    "job_index": event.get("job_index"),
                    "kind": event.get("kind"),
                    "quiet_ms": event.get("quiet_ms"),
                    "percent": event.get("percent"),
                })
        if run_ended is not None:
            elapsed_s = run_ended - run_started if run_started is not None else (run_ended * 1000 - marker.get("at_ms", 0)) / 1000.0
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


def write_html(path, playback_audio, playback_name, audio_name, report, aligned_log, timing=None):
    import html

    duration_s = report["audio"]["duration_s"]
    marker = report["sync_marker"]
    expected_id = marker.get("expected_id") or "unknown"
    decoded_id = marker.get("decoded_id") or "not decoded"
    match = marker.get("matches_expected")
    match_label = "match" if match else ("mismatch" if match is False else "not compared")
    event_names = {"nominal_request": "Nominal run request", "estimated_run_end": "Estimated run end", "job_start": "Job start", "quiet_timeout": "90-second quiet timeout", "silence_assumed": "Silence-assumed completion", "reported_complete": "Device-reported completion"}
    log_rows = "".join(f'<tr><td>{event.get("job_index") or ""}</td><td>{html.escape(event_names.get(event["kind"], event["kind"].replace("_", " ")))}</td><td>{event["audio_time_s"]:.3f}</td><td>{event.get("percent") or ""}</td></tr>' for event in aligned_log.get("events", []))
    timing_rows = "".join(f"<tr><th>{html.escape(name.replace('_', ' ').title())}</th><td>{value} ms</td></tr>" for name, value in (timing or {}).items())
    if aligned_log.get("marker_found"):
        trace_summary = "Receive trace present." if aligned_log.get("trace_found") else "No receive trace was recorded, so per-job and quiet-timeout markers are unavailable."
        log_summary = f'Matched attempt {html.escape(str(aligned_log.get("attempt_id")))} to recording ID {html.escape(str(aligned_log.get("recording_id")))}. Outcome: {html.escape(str(aligned_log.get("software_outcome") or "not recorded"))}. {trace_summary} API start/end timestamps have one-second resolution; the end marker is duration-aligned from the nominal request.'
    else:
        log_summary = "No matching sync marker found in the supplied run log."
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
    document = f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Audio run analysis: {html.escape(audio_name)}</title>
<style>
body{{font:15px system-ui,sans-serif;max-width:1280px;margin:24px auto;padding:0 18px;color:#18232b;background:#f4f6f7}}
h1{{font-size:24px}} .meta{{color:#40515c}} .pill{{display:inline-block;padding:4px 8px;border:1px solid #71808a;border-radius:4px;background:#fff}}
audio{{width:100%;margin:12px 0}} .log-summary{{margin:12px 0;padding:9px 12px;border-left:3px solid #167d89;background:#fff;color:#18232b}}
section{{margin-top:24px}} table{{border-collapse:collapse;background:#fff}} th,td{{padding:6px 12px;border:1px solid #c5cdd2;text-align:left}} code{{font-family:ui-monospace,monospace}}
</style>
<h1>Audio run analysis</h1>
<p class="meta">{html.escape(audio_name)} · {duration_s:.2f} s · {html.escape(str(report["audio"].get("codec")))} · {report["audio"].get("sample_rate")} Hz</p>
<p><span class="pill">Expected ID: {html.escape(expected_id)}</span> <span class="pill">Decoded ID: {html.escape(decoded_id)} ({match_label})</span> <span class="pill">Attempt: {html.escape(str(marker.get("attempt_id") or "not supplied"))}</span></p>
<audio controls preload="metadata"{audio_src}>Audio playback is not supported by this browser.</audio>
{audio_picker}
<p class="meta">The recording is provided for operator playback. This report presents sync matching, run-log events and measured timings only; it does not infer motion or laser state from sound.</p>
<p class="log-summary">{log_summary}</p>
<section><h2>Run-log alignment</h2><table><thead><tr><th>Job</th><th>Event</th><th>Audio time (s)</th><th>Progress</th></tr></thead><tbody>{log_rows}</tbody></table></section>
{f'<section><h2>Analyzer processing time</h2><table><thead><tr><th>Phase</th><th>Time</th></tr></thead><tbody>{timing_rows}</tbody></table><p class="meta">These analyzer-side durations exclude recording time and the Pi-to-analyzer network transfer.</p></section>' if timing_rows else ''}
<p class="meta">{html.escape(report["interpretation_limit"])}</p>
<footer>Copyright (c) 2026 Sean Donnellan</footer>
<script>
const player=document.querySelector('audio'), picker=document.getElementById('audio-file');
if(picker) picker.addEventListener('change',()=>{{if(picker.files.length){{player.src=URL.createObjectURL(picker.files[0]);player.load();}}}});
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
            "matches_expected": decoded_id == expected_id if expected_id else None,
            "preamble_tones": preamble[:3],
            "digit_tones": digits[:6],
            "cue_start_s": cue_start,
            "cue_to_request_ms": args.cue_to_request_ms,
            "nominal_run_request_s": run_request_s,
            "attempt_id": args.attempt_id,
            "marker_at_ms": args.marker_at_ms,
            "marker_minus_file_creation_s": round(args.marker_at_ms / 1000.0 - creation_epoch_ms / 1000.0, 3) if args.marker_at_ms is not None and creation_epoch_ms is not None else None,
        },
        "run_log_alignment": alignment,
        "interpretation_limit": "The recording is provided for operator playback; this report presents sync matching, run-log events and measured timings only. It does not infer motion or laser state from sound.",
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
    write_html(report_path, playback_audio, playback_name, args.recording.name, report, alignment, timing)
    timing["report_write_ms"] = (time.perf_counter_ns() - phase_started) // 1_000_000
    timing["analyzer_total_ms"] = timing["analysis_compute_ms"] + timing["report_write_ms"]
    report["timing"] = timing
    report["html_report"] = report_path.name
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()