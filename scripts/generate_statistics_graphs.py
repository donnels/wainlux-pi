#!/usr/bin/env python3
"""
K6 Statistics Visualizer - Generate graphs from burn_k6_image.py CSV logs
Creates 5 graph types with PlantUML-style gradient backgrounds
"""

import csv
import argparse
import re
import matplotlib.pyplot as plt
import numpy as np


def create_gradient_background(ax):
    """Apply PlantUML-style gradient: #ffffff (top-left) to #aaaaff (bottom-right)"""
    # Create gradient from white to light blue diagonal
    gradient = np.zeros((100, 100, 4))
    for i in range(100):
        for j in range(100):
            # Diagonal gradient
            t = (i + j) / 200.0
            r = 1.0 * (1 - t) + 0.67 * t  # FF -> AA
            g = 1.0 * (1 - t) + 0.67 * t  # FF -> AA
            b = 1.0  # FF
            gradient[i, j] = [r, g, b, 1.0]

    ax.imshow(
        gradient,
        extent=[ax.get_xlim()[0], ax.get_xlim()[1], ax.get_ylim()[0], ax.get_ylim()[1]],
        aspect="auto",
        zorder=0,
        origin="upper",
    )


def load_csv(filename):
    """Load CSV data into structured dict"""
    data = {
        "burn_start": None,
        "timestamps": [],
        "elapsed": [],
        "phases": [],
        "operations": [],
        "durations": [],
        "bytes": [],
        "cumulative": [],
        "throughput": [],
        "status_pct": [],
        "states": [],
        "response_types": [],
    }

    with open(filename, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if data["burn_start"] is None:
                data["burn_start"] = row["burn_start"]
            data["timestamps"].append(row["timestamp"])
            data["elapsed"].append(float(row["elapsed_s"]))
            data["phases"].append(row["phase"])
            data["operations"].append(row["operation"])
            data["durations"].append(float(row["duration_ms"]))
            data["bytes"].append(int(row["bytes_transferred"]))
            data["cumulative"].append(int(row["cumulative_bytes"]))
            data["throughput"].append(
                float(row["throughput_kbps"]) if row["throughput_kbps"] else 0
            )
            # Handle status_pct - may be empty, numeric, or text (ACK, IDLE, etc)
            try:
                pct = int(row["status_pct"]) if row["status_pct"] else None
            except ValueError:
                pct = None  # Non-numeric values like 'ACK', 'IDLE'
            data["status_pct"].append(pct)
            # Handle new columns (may not exist in older CSVs)
            data["states"].append(row.get("state", "ACTIVE"))
            data["response_types"].append(row.get("response_type", ""))

    return data


def graph_timeline(data, output, burn_start):
    """Timeline/Gantt chart showing where time is spent"""
    fig, ax = plt.subplots(figsize=(14, 6))

    # Group by phase and operation
    phases = {"SETUP": [], "BUILD": [], "DATA": [], "BURN": []}
    for i, phase in enumerate(data["phases"]):
        if phase not in phases:
            phases[phase] = []
        phases[phase].append(
            (data["elapsed"][i], data["durations"][i], data["operations"][i])
        )

    y_pos = 0
    colors = {
        "SETUP": "#ff9999",
        "BUILD": "#ffcc99",
        "DATA": "#99ff99",
        "BURN": "#9999ff",
    }

    for phase, items in phases.items():
        if not items:
            continue
        for start, duration, op in items:
            width = duration / 1000.0  # Convert ms to seconds
            ax.barh(
                y_pos,
                width,
                left=start,
                height=0.8,
                color=colors.get(phase, "#cccccc"),
                edgecolor="black",
                linewidth=0.5,
            )

            # Label key operations
            if "HOME" in op or "DATA chunk 0" in op or ("STATUS" in op and "37" in op):
                ax.text(
                    start + width / 2,
                    y_pos,
                    op.split()[0],
                    ha="center",
                    va="center",
                    fontsize=7,
                    weight="bold",
                )

        y_pos += 1

    ax.set_ylim(-0.5, y_pos - 0.5)
    ax.set_yticks(list(range(len([p for p in phases if phases[p]]))))
    ax.set_yticklabels([p for p in phases.keys() if phases[p]])
    ax.set_xlabel("Time (seconds)", fontsize=12, weight="bold")
    ax.set_title(f"K6 Protocol Timeline\n{burn_start}", fontsize=14, weight="bold")
    ax.grid(axis="x", alpha=0.3, linestyle="--")

    create_gradient_background(ax)
    plt.tight_layout()
    plt.savefig(output, dpi=150, bbox_inches="tight")
    print(f"Created: {output}")
    plt.close()


def graph_throughput(data, output, burn_start):
    """Throughput over time during DATA phase"""
    fig, ax = plt.subplots(figsize=(12, 6))

    # Filter DATA phase only
    data_elapsed = []
    data_throughput = []
    for i, phase in enumerate(data["phases"]):
        if phase == "DATA" and data["bytes"][i] > 0:
            data_elapsed.append(data["elapsed"][i])
            data_throughput.append(data["throughput"][i])

    if data_elapsed:
        ax.plot(
            data_elapsed,
            data_throughput,
            color="#3366cc",
            linewidth=2,
            marker="o",
            markersize=3,
        )
        ax.fill_between(data_elapsed, data_throughput, alpha=0.3, color="#3366cc")

        # Stats
        avg = np.mean(data_throughput)
        ax.axhline(
            avg,
            color="red",
            linestyle="--",
            linewidth=2,
            label=f"Average: {avg:.1f} KB/s",
        )

    ax.set_xlabel("Time (seconds)", fontsize=12, weight="bold")
    ax.set_ylabel("Throughput (KB/s)", fontsize=12, weight="bold")
    ax.set_title(f"Data Transfer Throughput\\n{burn_start}", fontsize=14, weight="bold")
    ax.legend(loc="upper right", fontsize=10)
    ax.grid(alpha=0.3, linestyle="--")

    create_gradient_background(ax)
    plt.tight_layout()
    plt.savefig(output, dpi=150, bbox_inches="tight")
    print(f"Created: {output}")
    plt.close()


def graph_status_timing(data, output, burn_start):
    """Status frame intervals during burn"""
    fig, ax = plt.subplots(figsize=(12, 6))

    # Filter BURN phase status frames
    burn_pct = []
    burn_delta = []
    for i, phase in enumerate(data["phases"]):
        if phase == "BURN" and data["status_pct"][i] is not None:
            if data["durations"][i] > 0:  # Skip first (no delta)
                burn_pct.append(data["status_pct"][i])
                burn_delta.append(data["durations"][i])

    if burn_pct:
        ax.plot(
            burn_pct, burn_delta, color="#cc3366", linewidth=2, marker="o", markersize=6
        )

        # Stats
        if burn_delta:
            avg = np.mean(burn_delta)
            ax.axhline(
                avg,
                color="green",
                linestyle="--",
                linewidth=2,
                label=f"Average: {avg:.0f}ms",
            )

    ax.set_xlabel("Burn Progress (%)", fontsize=12, weight="bold")
    ax.set_ylabel("Time Between Status Frames (ms)", fontsize=12, weight="bold")
    ax.set_title(
        f"Laser Burn Status Frame Timing\\n{burn_start}", fontsize=14, weight="bold"
    )
    ax.legend(loc="upper right", fontsize=10)
    ax.grid(alpha=0.3, linestyle="--")

    create_gradient_background(ax)
    plt.tight_layout()
    plt.savefig(output, dpi=150, bbox_inches="tight")
    print(f"Created: {output}")
    plt.close()


def graph_chunk_histogram(data, output, burn_start):
    """Histogram of chunk transmission times"""
    fig, ax = plt.subplots(figsize=(10, 6))

    # Filter DATA chunks
    chunk_times = []
    for i, op in enumerate(data["operations"]):
        if "DATA chunk" in op or "CHUNK" in op:
            chunk_times.append(data["durations"][i])

    if chunk_times:
        bins = np.arange(min(chunk_times), max(chunk_times) + 2, 1)
        n, bins, patches = ax.hist(
            chunk_times, bins=bins, color="#66cc66", edgecolor="black", linewidth=1.5
        )

        # Stats
        avg = np.mean(chunk_times)
        std = np.std(chunk_times)
        ax.axvline(
            avg, color="red", linestyle="--", linewidth=2, label=f"Mean: {avg:.1f}ms"
        )
        ax.axvline(
            avg - std,
            color="orange",
            linestyle=":",
            linewidth=2,
            label=f"Std: ±{std:.1f}ms",
        )
        ax.axvline(avg + std, color="orange", linestyle=":", linewidth=2)

        # Annotate highest bar
        max_idx = np.argmax(n)
        ax.text(
            bins[max_idx],
            n[max_idx],
            f"{int(n[max_idx])}",
            ha="center",
            va="bottom",
            fontsize=10,
            weight="bold",
        )

    ax.set_xlabel("Chunk Transmission Time (ms)", fontsize=12, weight="bold")
    ax.set_ylabel("Count", fontsize=12, weight="bold")
    ax.set_title(
        f"DATA Chunk Timing Distribution\\n{burn_start}", fontsize=14, weight="bold"
    )
    ax.legend(loc="upper right", fontsize=10)
    ax.grid(axis="y", alpha=0.3, linestyle="--")

    create_gradient_background(ax)
    plt.tight_layout()
    plt.savefig(output, dpi=150, bbox_inches="tight")
    print(f"Created: {output}")
    plt.close()


def graph_operation_bars(data, output, burn_start):
    """Bar chart of major operation durations"""
    fig, ax = plt.subplots(figsize=(12, 8))

    # Aggregate by operation type
    ops = {}
    for i, op in enumerate(data["operations"]):
        # Simplify operation names
        if "DATA chunk" in op or "CHUNK" in op:
            key = "DATA_CHUNKS"
        elif "STATUS" in op:
            key = "BURN_PHASE"
        else:
            key = op.split()[0]

        if key not in ops:
            ops[key] = []
        ops[key].append(data["durations"][i])

    # Sum totals
    op_totals = {k: sum(v) / 1000.0 for k, v in ops.items()}  # Convert to seconds

    # Sort by duration
    sorted_ops = sorted(op_totals.items(), key=lambda x: x[1], reverse=True)
    labels = [x[0] for x in sorted_ops]
    values = [x[1] for x in sorted_ops]

    colors_map = {
        "HOME": "#ff6666",
        "DATA_CHUNKS": "#66ff66",
        "BURN_PHASE": "#6666ff",
        "CONNECT": "#ffcc66",
        "VERSION": "#cc66ff",
        "STOP": "#66ccff",
        "FRAMING": "#ffff66",
        "INIT": "#ff66cc",
        "CHUNK": "#99ff99",
    }
    colors = [colors_map.get(label, "#cccccc") for label in labels]

    bars = ax.barh(labels, values, color=colors, edgecolor="black", linewidth=1.5)

    # Annotate bars
    for i, bar in enumerate(bars):
        width = bar.get_width()
        ax.text(
            width,
            bar.get_y() + bar.get_height() / 2,
            f" {width:.1f}s",
            ha="left",
            va="center",
            fontsize=10,
            weight="bold",
        )

    ax.set_xlabel("Total Time (seconds)", fontsize=12, weight="bold")
    ax.set_title(
        f"Operation Duration Breakdown\\n{burn_start}", fontsize=14, weight="bold"
    )
    ax.grid(axis="x", alpha=0.3, linestyle="--")

    create_gradient_background(ax)
    plt.tight_layout()
    plt.savefig(output, dpi=150, bbox_inches="tight")
    print(f"Created: {output}")
    plt.close()


def main():
    parser = argparse.ArgumentParser(
        description="Generate K6 burn statistics graphs from CSV"
    )
    parser.add_argument("csv", help="Input CSV file from burn_k6_image.py")
    parser.add_argument(
        "--output-prefix",
        help="Output filename prefix (default: derived from CSV filename)",
    )
    parser.add_argument(
        "--graphs",
        nargs="+",
        choices=["timeline", "throughput", "status", "histogram", "bars", "all"],
        default=["all"],
        help="Which graphs to generate",
    )
    args = parser.parse_args()

    # Derive output prefix from CSV filename if not provided
    if args.output_prefix:
        prefix = args.output_prefix
    else:
        # Extract timestamp from stat-YY-MM-DD-HH-MM.csv format
        match = re.search(r"stat-(\d{2}-\d{2}-\d{2}-\d{2}-\d{2})", args.csv)
        if match:
            prefix = f"stat-{match.group(1)}"
        else:
            prefix = "k6_stats"

    print(f"Loading {args.csv}...")
    data = load_csv(args.csv)
    print(f"Loaded {len(data['elapsed'])} operations")
    burn_start = data["burn_start"] if data["burn_start"] else "Unknown"

    graphs_to_generate = args.graphs
    if "all" in graphs_to_generate:
        graphs_to_generate = ["timeline", "throughput", "status", "histogram", "bars"]

    if "timeline" in graphs_to_generate:
        graph_timeline(data, f"{prefix}_timeline.png", burn_start)

    if "throughput" in graphs_to_generate:
        graph_throughput(data, f"{prefix}_throughput.png", burn_start)

    if "status" in graphs_to_generate:
        graph_status_timing(data, f"{prefix}_status.png", burn_start)

    if "histogram" in graphs_to_generate:
        graph_chunk_histogram(data, f"{prefix}_histogram.png", burn_start)

    if "bars" in graphs_to_generate:
        graph_operation_bars(data, f"{prefix}_bars.png", burn_start)

    print("Done!")


if __name__ == "__main__":
    main()
