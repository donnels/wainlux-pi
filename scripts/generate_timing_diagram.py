#!/usr/bin/env python3
"""
DRAFT - Generate PlantUML timing diagram from K6 burn CSV statistics.

WARNING: This script is a work in progress. Known issues:
- End of diagram may have overlapping or out-of-order events
- RX timing calculation needs refinement for long-duration STATUS events
- Final state positioning is imperfect
- Time labels on x-axis overlap for long burns

Usage: ./generate_timing_diagram.py stat-26-01-21-23-26.csv > timing.puml
"""

import csv
import sys
import argparse


def load_csv(filename):
    """Load CSV and return list of dicts"""
    with open(filename, "r") as f:
        return list(csv.DictReader(f))


def write_phase_change(out, main_phase, last_phase):
    """Write phase change if different from last."""
    if main_phase != last_phase:
        out.write(f'phase is "{main_phase}"\n')
    return main_phase


def write_lane_changes(out, is_build, is_data, last_build, last_data):
    """Write BUILD/DATA lane changes."""
    if is_build != last_build:
        out.write(f"build is {'high' if is_build else 'low'}\n")
    if is_data != last_data:
        out.write(f"data_lane is {'high' if is_data else 'low'}\n")
    return is_build, is_data


def write_device_response(out, response, status_pct, last_progress):
    """Write device response state."""
    if response == "TIMEOUT":
        out.write("device is TIMEOUT\n")
    elif "HEARTBEAT" in response:
        out.write("device is HEARTBEAT\n")
    elif response == "ACK":
        out.write("device is ACK\n")
    elif response == "STATUS":
        out.write("device is STATUS\n")
        if status_pct:
            progress_str = f"{status_pct}%"
            if progress_str != last_progress:
                out.write(f'progress is "{progress_str}"\n')
                return progress_str
    return last_progress


def generate_timing_diagram(data, output):
    """Generate PlantUML timing diagram from CSV data"""

    if not data:
        print("No data to process", file=sys.stderr)
        return

    # Get time bounds
    start_time = float(data[0]["elapsed_s"])
    end_time = float(data[-1]["elapsed_s"])
    duration = end_time - start_time

    burn_start = data[0]["burn_start"]

    out = output

    # PlantUML header
    out.write("@startuml\n")
    out.write(
        f"title K6 Laser Burn Timing Diagram\\n{burn_start}\\nTotal Duration: {duration:.1f}s\\n\n"
    )
    out.write(
        "scale 1 as 50 pixels\n"
    )  # Increased from 10 to reduce time label overlap
    out.write('concise "Phase" as phase\n')
    out.write('binary "BUILD" as build\n')
    out.write('binary "DATA" as data_lane\n')
    out.write('binary "Serial TX" as tx\n')
    out.write('binary "Serial RX" as rx\n')
    out.write('robust "Device Response" as device\n')
    out.write('concise "Burn Progress" as progress\n\n')

    # Define device response states
    out.write("device has IDLE,ACK,HEARTBEAT,STATUS,TIMEOUT\n\n")

    # Time 0
    out.write("@0\n")
    out.write("phase is {-}\n")
    out.write("build is low\n")
    out.write("data_lane is low\n")
    out.write("tx is low\n")
    out.write("rx is low\n")
    out.write("device is IDLE\n")
    out.write('progress is "0%"\n\n')

    last_phase = None
    last_progress = "0%"
    last_build = False
    last_data = False
    max_time = 0.0  # Track the latest event time

    for i, row in enumerate(data):
        t = float(row["elapsed_s"])
        phase_val = row["phase"]
        # Map BUILD/DATA to high-level UPLOAD phase, but track separately
        main_phase = "UPLOAD" if phase_val in ("BUILD", "DATA") else phase_val
        is_build = phase_val == "BUILD"
        is_data = phase_val == "DATA"
        response = row["response_type"] if row["response_type"] else "ACK"
        operation = row["operation"]
        duration_ms = float(row["duration_ms"])
        status_pct = row["status_pct"]

        # Get next event time for capping RX timing
        next_t = (
            float(data[i + 1]["elapsed_s"])
            if i + 1 < len(data)
            else t + (duration_ms / 1000) + 0.1
        )

        # Time marker
        out.write(f"@{t:.3f}\n")

        # Phase changes (use main_phase for high-level view)
        last_phase = write_phase_change(out, main_phase, last_phase)

        # BUILD/DATA lane changes
        last_build, last_data = write_lane_changes(
            out, is_build, is_data, last_build, last_data
        )

        # Serial activity (TX happens at start, RX after duration)
        if "CHUNK" not in operation and "STATUS" not in operation:
            out.write("tx is high\n")
            out.write("device is IDLE\n")

        # RX activity after command - cap at 0.01s before next event to avoid overlap
        t_rx_raw = t + (duration_ms / 1000)
        t_rx = min(t_rx_raw, next_t - 0.02)

        # Only show RX if it's after current time
        if t_rx > t:
            out.write(f"@{t_rx:.3f}\n")
            out.write("tx is low\n")
            out.write("rx is high\n")

            # Device response
            last_progress = write_device_response(
                out, response, status_pct, last_progress
            )

            # Brief RX low after response
            t_rx_end = min(t_rx + 0.01, next_t - 0.01)
            out.write(f"@{t_rx_end:.3f}\n")
            out.write("rx is low\n")
            out.write("device is IDLE\n")

            # Track max time for final state
            max_time = max(max_time, t_rx_end)

    # Final state - 1 second after the last actual event
    final_time = max(max_time, end_time) + 1.0
    out.write(f"@{final_time:.3f}\n")
    out.write("phase is {-}\n")
    out.write("build is low\n")
    out.write("data_lane is low\n")
    out.write("tx is low\n")
    out.write("rx is low\n")
    out.write("device is IDLE\n")

    out.write("\n@enduml\n")


def main():
    parser = argparse.ArgumentParser(
        description="Generate PlantUML timing diagram from K6 CSV"
    )
    parser.add_argument("csv", help="Input CSV file (stat-*.csv)")
    parser.add_argument("-o", "--output", help="Output .puml file (default: stdout)")
    parser.add_argument(
        "--sample", type=int, help="Sample every Nth row for large datasets"
    )
    args = parser.parse_args()

    print(f"Loading {args.csv}...", file=sys.stderr)
    data = load_csv(args.csv)
    print(f"Loaded {len(data)} rows", file=sys.stderr)

    if args.sample and args.sample > 1:
        print(f"Sampling every {args.sample} rows...", file=sys.stderr)
        data = data[:: args.sample]
        print(f"Sampled to {len(data)} rows", file=sys.stderr)

    if args.output:
        with open(args.output, "w") as f:
            generate_timing_diagram(data, f)
        print(f"Created: {args.output}", file=sys.stderr)
    else:
        generate_timing_diagram(data, sys.stdout)


if __name__ == "__main__":
    main()
