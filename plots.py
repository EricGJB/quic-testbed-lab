"""Build report figures from published tables and the included telemetry traces."""

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

COLORS = {"cubic": "#176487", "reno": "#c86121", "tcp": "#4d5062",
          "cubic_small_burst": "#168875", "cubic_pkt12": "#9d4372"}
LABELS = {"cubic": "aioquic CUBIC", "reno": "aioquic Reno", "tcp": "TCP CUBIC + TLS",
          "cubic_small_burst": "CUBIC, 2-packet burst", "cubic_pkt12": "CUBIC, loss threshold 12"}


def read(path):
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def finish(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=160, facecolor="white")
    plt.close(fig)


def step(results, raw):
    data = read(results / "goodput_100ms.csv")
    runs = defaultdict(list)
    for row in data:
        if row["profile"] == "step":
            runs[row["algorithm"], int(row["start_ms"])].append(float(row["goodput_mbps"]))
    fig, (ax, window) = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    for algorithm in LABELS:
        xy = [(stamp / 1000, statistics.median(values))
              for (name, stamp), values in sorted(runs.items()) if name == algorithm]
        if xy:
            ax.plot(*zip(*xy), label=LABELS[algorithm], color=COLORS[algorithm])
    for pane in (ax, window):
        pane.axvspan(1.8, 3.8, color="#d8bc76", alpha=.3)
        pane.grid(alpha=.25)
    ax.set(title="Capacity step, 40 to 5 to 40 Mbit/s", ylabel="Receiver goodput (Mbit/s)")
    ax.legend(ncol=2, fontsize=8, loc="upper right")
    example = raw / "bulk__cubic__step__00"
    start = json.loads((example / "run.json").read_text())["start"]
    samples = [r for r in json.loads((example / "sent.json").read_text())["samples"]
               if 0 <= r[0] - start < 5.8]
    t = [r[0] - start for r in samples]
    window.plot(t, [r[1]/1024 for r in samples], label="cwnd", color=COLORS["cubic"])
    window.plot(t, [r[2]/1024 for r in samples], label="Bytes in flight", color="#c86121", alpha=.8)
    window.set(xlabel="Time after workload start (s)", ylabel="KiB", xlim=(0, 5.8),
               title="Sender state, aioquic CUBIC repeat 0")
    window.legend(loc="upper right", fontsize=8)
    finish(fig, results / "step.png")


def bars(results, followup):
    rows = [r for r in read(results / "transport_summary.csv") if r["workload"] == "bulk"]
    if followup:
        rows += [r for r in read(followup / "transport_summary.csv") if r["workload"] == "bulk"]
    lookup = {(r["profile"], r["algorithm"]): r for r in rows}
    profiles = ["fixed40", "fixed22", "rate10ms", "rate20ms", "rate100ms",
                "delay10ms", "delay20ms", "delay100ms", "delayjitter", "ack_jitter", "loss1", "urban"]
    fig, ax = plt.subplots(figsize=(11, 5.8))
    for index, (algorithm, label) in enumerate(LABELS.items()):
        points = [(y, lookup[profile, algorithm]) for y, profile in enumerate(profiles)
                  if (profile, algorithm) in lookup]
        ax.barh([y + (index - 2)*.15 for y, _ in points],
                [float(row["goodput_mbps"]) for _, row in points], height=.15,
                label=label, color=COLORS[algorithm])
    ax.set(yticks=list(range(len(profiles))), yticklabels=profiles, xlabel="Receiver goodput (Mbit/s)",
           title="Bulk goodput by network condition, medians across runs")
    ax.invert_yaxis()
    ax.grid(axis="x", alpha=.25)
    ax.legend(loc="lower right", fontsize=8)
    finish(fig, results / "bulk.png")


def media(results):
    rows = [r for r in read(results / "grace_summary.csv") if r["algorithm"] == "cubic"]
    profiles = list(dict.fromkeys(r["profile"] for r in rows))
    lookup = {(r["profile"], int(r["deadline_ms"])): r for r in rows}
    fig, ax = plt.subplots(figsize=(9, 5.5))
    for index, (deadline, color) in enumerate(zip((50, 100, 200), ("#b85538", "#177493", "#167858"))):
        ax.barh([p+(index-1)*.22 for p in range(len(profiles))],
                [float(lookup[profile, deadline]["on_time_block_pct"]) for profile in profiles],
                height=.21, label=f"{deadline} ms", color=color)
    ax.set(yticks=list(range(len(profiles))), yticklabels=profiles, xlim=(0, 100),
           xlabel="Complete feature blocks delivered before deadline (%)",
           title="GRACE-4096 over QUIC DATAGRAM, aioquic CUBIC")
    ax.invert_yaxis()
    ax.grid(axis="x", alpha=.25)
    ax.legend(loc="lower right")
    finish(fig, results / "grace.png")


def reorder(results, followup):
    original = read(results / "transport_summary.csv")
    modified = read(followup / "transport_summary.csv")
    profiles = ("fixed40", "delay10ms", "delayjitter", "urban", "loss1")
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), sharey=True)
    for index, (algorithm, data) in enumerate((("cubic", original), ("cubic_pkt12", modified))):
        rows = {r["profile"]: r for r in data if r["algorithm"] == algorithm and r["workload"] == "bulk"}
        y = [i+(index-.5)*.33 for i in range(len(profiles))]
        for ax, key in zip(axes, ("goodput_mbps", "declared_lost_but_received")):
            ax.barh(y, [float(rows[name][key]) for name in profiles], height=.31,
                    color=COLORS[algorithm], label=LABELS[algorithm])
            ax.grid(axis="x", alpha=.25)
    axes[0].set(yticks=list(range(len(profiles))), yticklabels=profiles, xlabel="Receiver goodput (Mbit/s)")
    axes[0].invert_yaxis()
    axes[1].set(xlabel="1-RTT packets marked lost but received (count)")
    axes[0].legend(fontsize=8, loc="lower right")
    fig.suptitle("Changing only the sender packet-number loss threshold")
    finish(fig, results / "reorder.png")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("raw", type=Path)
    parser.add_argument("--followup", type=Path)
    args = parser.parse_args()
    step(args.results, args.raw)
    bars(args.results, args.followup)
    if args.followup:
        reorder(args.results, args.followup)
    if (args.results / "grace_summary.csv").exists():
        media(args.results)


if __name__ == "__main__":
    main()
