"""Summarize application delivery, transport state and packet timing independently."""

import argparse
import csv
import gzip
import json
import statistics
import struct
from collections import Counter, defaultdict, deque
from pathlib import Path


def quantile(values, p):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * p
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def get(path):
    return json.loads(path.read_text())


def csv_write(path, rows):
    if not rows:
        return
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def packets(path, origin):
    """Read tcpdump's Ethernet captures, with times expressed relative to workload start."""
    with gzip.open(path, "rb") as handle:
        header = handle.read(24)
        magic = {
            b"\x4d\x3c\xb2\xa1": ("<", 1e-9), b"\xa1\xb2\x3c\x4d": (">", 1e-9),
            b"\xd4\xc3\xb2\xa1": ("<", 1e-6), b"\xa1\xb2\xc3\xd4": (">", 1e-6),
        }
        endian, resolution = magic[header[:4]]
        if struct.unpack_from(endian + "I", header, 20)[0] != 1:
            raise ValueError("expected Ethernet pcap")
        while record := handle.read(16):
            seconds, fraction, captured, _ = struct.unpack(endian + "IIII", record)
            data = handle.read(captured)
            if len(data) != captured:
                raise ValueError("truncated capture")
            if data[12:14] != b"\x08\x00":
                continue
            ip = data[14:]
            proto = ip[9]
            if proto not in (6, 17):
                continue
            direction = "forward" if ip[12:16] == b"\x0a\xce\x00\x02" else "reverse"
            size = int.from_bytes(ip[2:4], "big")
            # Exclude mutable IP TTL and checksums. Retain identity and ciphertext prefix.
            transport = ip[(ip[0] & 15) * 4:]
            if proto == 17:
                key = ip[4:6] + transport[:6] + transport[8:52]
            else:
                key = ip[4:6] + transport[:16] + transport[18:54]
            yield (seconds + fraction * resolution - origin, direction, size, key)


def packet_metrics(path, run):
    origin = run["start"] + run["wall_minus_mono"]
    duration = run["duration"]
    tx = list(packets(path / "tx.pcap.gz", origin))
    rx = list(packets(path / "rx.pcap.gz", origin))
    forward_tx = [row for row in tx if row[1] == "forward" and 0 <= row[0] < duration]
    forward_rx = [row for row in rx if row[1] == "forward" and 0 <= row[0] < duration]
    reverse_rx = [row for row in tx if row[1] == "reverse" and 0 <= row[0] < duration]
    arrival = defaultdict(deque)
    for timestamp, direction, _, key in rx:
        if direction == "forward":
            arrival[key].append(timestamp)
    transit = []
    for timestamp, _, _, key in forward_tx:
        dates = arrival[key]
        while dates and dates[0] < timestamp:
            dates.popleft()
        if dates:
            transit.append((dates.popleft() - timestamp) * 1000)

    def gaps(rows, min_size):
        stamps = [row[0] for row in rows if row[2] >= min_size]
        intervals = [(b-a)*1000 for a, b in zip(stamps, stamps[1:])]
        return quantile(intervals, .5), quantile(intervals, .95), quantile(
            Counter(int(t * 1000) for t in stamps).values(), .95
        )

    tx50, tx95, txburst = gaps(forward_tx, 1000)
    rx50, rx95, rxburst = gaps(forward_rx, 1000)
    ack50, ack95, ackburst = gaps(reverse_rx, 0)
    return dict(tx_ip_bytes=sum(row[2] for row in forward_tx), rx_ip_bytes=sum(row[2] for row in forward_rx),
                forward_tx_packets=len(forward_tx), forward_rx_packets=len(forward_rx),
                transit_matched=len(transit), transit_p50_ms=quantile(transit, .5),
                transit_p95_ms=quantile(transit, .95), tx_gap_p50_ms=tx50, tx_gap_p95_ms=tx95,
                rx_gap_p50_ms=rx50, rx_gap_p95_ms=rx95, tx_nonempty_ms_burst_p95=txburst,
                rx_nonempty_ms_burst_p95=rxburst, reverse_gap_p50_ms=ack50, reverse_gap_p95_ms=ack95,
                reverse_nonempty_ms_burst_p95=ackburst)


def kernel_metrics(run):
    def field(state, dev, key):
        qdiscs = [q for q in state[2] if q["dev"] == dev]
        leaf = next((q for q in qdiscs if q["kind"] == "netem"), None)
        if not leaf:
            leaf = next((q for q in qdiscs if q.get("root")), {})
        return leaf.get(key, 0)
    out = {}
    for side, device in (("forward", "r1"), ("reverse", "r0")):
        out[side + "_qdisc_drops"] = field(run["qdisc_after"], device, "drops") - field(
            run["qdisc_before"], device, "drops")
        out[side + "_sampled_backlog_max_bytes"] = max(
            (field(sample, device, "backlog") for sample in run["qdisc_samples"]), default=0)
    switches = run["switches"]
    out["switches"] = len(switches)
    if switches:
        out["switch_commit_lag_p95_ms"] = quantile([done - planned for planned, _, done, _, _ in switches], .95)
        out["switch_interval_p50_ms"] = quantile([b[2] - a[2] for a, b in zip(switches, switches[1:])], .5)
    return out


def transport_metrics(path, run):
    start, stop = run["start"], run["start"] + run["duration"]
    sent = get(path / "sent.json")
    samples = [row for row in sent.get("samples", []) if start <= row[0] < stop]
    wakes = [(fired - target) * 1000 for target, fired in sent.get("pacing_wakes", []) if start <= target < stop]
    result = dict(cwnd_bytes_p50=quantile([row[1] for row in samples], .5),
                  inflight_bytes_p50=quantile([row[2] for row in samples], .5),
                  srtt_p50_ms=quantile([row[3] * 1000 for row in samples if row[3] > 0], .5),
                  srtt_p95_ms=quantile([row[3] * 1000 for row in samples if row[3] > 0], .95),
                  sample_count=len(samples),
                  cwnd_low_credit_fraction=sum(row[6] for row in samples)/len(samples) if samples else None,
                  pending_fraction=sum(row[5] for row in samples)/len(samples) if samples else None,
                  pacing_waits=sum(start <= row[0] < stop for row in sent.get("pacing_waits", [])),
                  pacing_wakes=len(wakes), pacing_wake_lag_p95_ms=quantile(wakes, .95),
                  pto_count_max=max((row[7] for row in samples), default=None))
    result["pacer_target_mbps_p50"] = quantile([1200 * 8 / row[4] / 1e6 for row in samples if row[4]], .5)
    if run["profile"] in ("step", "step_shallow") and len(run["switches"]) == 2 and samples:
        down, up = [start + row[2] / 1000 for row in run["switches"]]
        phases = {"before": (start + .8, down), "first_250ms_low": (down, down + .25),
                  "last_500ms_low": (up - .5, up), "first_500ms_recovery": (up, up + .5)}
        for phase, (lo, hi) in phases.items():
            selected = [row for row in samples if lo <= row[0] < hi]
            result[f"cwnd_{phase}_bytes_p50"] = quantile([row[1] for row in selected], .5)
            result[f"pacer_{phase}_mbps_p50"] = quantile([1200 * 8 / row[4] / 1e6 for row in selected if row[4]], .5)
    log = path / "client.qlog.json.gz"
    if log.exists():
        with gzip.open(log, "rt") as handle:
            data = json.load(handle)
        t0 = (start + run["wall_minus_mono"]) * 1000
        events = [e for trace in data["traces"] for e in trace["events"]
                  if t0 <= e["time"] < t0 + run["duration"] * 1000]
        result["quic_declared_lost"] = sum(e["name"] == "recovery:packet_lost" for e in events)
        lost = {e["data"]["packet_number"]: e["time"] for e in events
                if e["name"] == "recovery:packet_lost" and e["data"]["type"] == "1RTT"}
        with gzip.open(path / "server.qlog.json.gz", "rt") as handle:
            peer = json.load(handle)
        matched = {}
        for trace in peer["traces"]:
            for event in trace["events"]:
                if event["name"] != "transport:packet_received":
                    continue
                header = event["data"]["header"]
                pn = header["packet_number"]
                if header["packet_type"] == "1RTT" and pn in lost:
                    matched.setdefault(pn, event["time"])
        result["declared_lost_1rtt"] = len(lost)
        result["declared_lost_but_received"] = len(matched)
        result["declared_lost_then_received"] = sum(stamp > lost[pn] for pn, stamp in matched.items())
        result["declared_lost_received_pct"] = 100 * len(matched) / len(lost) if lost else None
        frame_names = Counter(frame["frame_type"] for e in events
            if e["name"] == "transport:packet_sent" for frame in e["data"].get("frames", []))
        result["flow_blocked_frames"] = frame_names["data_blocked"] + frame_names["stream_data_blocked"]
    return result


def bulk_metrics(path, run):
    received = get(path / "received.json")["received"]
    start, duration = run["start"], run["duration"]

    def delivered(relative):
        return max((total for stamp, total in received if stamp <= start + relative), default=0)
    def rate(lo, hi):
        return (delivered(hi) - delivered(lo)) * 8 / (hi - lo) / 1e6

    end = duration
    warm = min(.8, end / 2)
    summary = dict(app_bytes=delivered(end)-delivered(0), goodput_mbps=rate(0, end),
                   steady_mbps=rate(warm, end), comparison_mbps=rate(warm, min(5.1, end)))
    switches = run["switches"]
    if run["profile"].startswith("rate") and len(switches) >= 2:
        first, last = switches[0][2]/1000, switches[-1][2]/1000
        weighted = sum((b[2]-a[2])/1000 * a[4] for a, b in zip(switches, switches[1:]))
        summary["oscillation_goodput_mbps"] = rate(first, last)
        summary["oscillation_nominal_mbps"] = weighted / (last-first)
        summary["oscillation_app_pct_of_nominal"] = 100 * summary["oscillation_goodput_mbps"] / summary["oscillation_nominal_mbps"]
    if run["profile"] in ("step", "step_shallow") and len(run["switches"]) == 2:
        down, up = [row[2] / 1000 for row in run["switches"]]
        summary.update(before_mbps=rate(.8, down), low_mbps=rate(down + .1, up),
                       recovery_mbps=rate(up, end), recovery_bytes=delivered(end)-delivered(up),
                       recovery_interval_ms=(end-up)*1000)
    series = []
    for index in range(int(duration * 10)):
        lo, hi = index/10, (index+1)/10
        series.append(dict(run=path.name, algorithm=run["algorithm"], profile=run["profile"],
                           repeat=run["run_index"], start_ms=index*100, goodput_mbps=rate(lo, hi)))
    return summary, series


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("raw", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    args.out.mkdir(exist_ok=True, parents=True)
    rows, series = [], []
    for path in sorted(args.raw.glob("*/run.json")):
        run = get(path)
        row = dict(run=path.parent.name, profile=run["profile"], algorithm=run["algorithm"],
                   workload=run["workload"], repeat=run["run_index"], duration_s=run["duration"])
        row.update(kernel_metrics(run))
        row.update(packet_metrics(path.parent, run))
        row.update(transport_metrics(path.parent, run))
        if run["workload"] == "bulk":
            extra, curve = bulk_metrics(path.parent, run)
            row.update(extra)
            series.extend(curve)
        rows.append(row)
    groups = defaultdict(list)
    for row in rows:
        groups[row["workload"], row["profile"], row["algorithm"]].append(row)
    summaries = []
    for (workload, profile, algorithm), group in sorted(groups.items()):
        row = dict(workload=workload, profile=profile, algorithm=algorithm, n=len(group))
        for name in group[0]:
            if name == "repeat":
                continue
            values = [g.get(name) for g in group if isinstance(g.get(name), (int, float))]
            if values:
                row[name] = statistics.median(values)
        if workload == "bulk":
            row["goodput_min_mbps"] = min(r["goodput_mbps"] for r in group)
            row["goodput_max_mbps"] = max(r["goodput_mbps"] for r in group)
        summaries.append(row)
    recovery = []
    by_run = defaultdict(list)
    for point in series:
        by_run[point["run"]].append(point)
    for row in rows:
        if row["profile"] not in ("step", "step_shallow") or "recovery_mbps" not in row:
            continue
        baseline = groups.get(("bulk", "fixed40", row["algorithm"]))
        if not baseline:
            continue
        target = statistics.median(item["steady_mbps"] for item in baseline)
        start_ms = 1000 * row["duration_s"] - row["recovery_interval_ms"]
        points = [item for item in by_run[row["run"]] if item["start_ms"] >= start_ms]
        crossing = next((points[i]["start_ms"] - start_ms for i in range(len(points)-2)
                         if all(p["goodput_mbps"] >= .9*target for p in points[i:i+3])), None)
        expected = target * 1e6 * row["recovery_interval_ms"] / 8000
        recovery.append(dict(run=row["run"], profile=row["profile"], algorithm=row["algorithm"],
            same_algorithm_fixed40_reference_mbps=target, recovery_interval_ms=row["recovery_interval_ms"],
            recovery_mbps=row["recovery_mbps"], shortfall_bytes=round(expected - row["recovery_bytes"]),
            first_sustained_90pct_ms=crossing, right_censored=crossing is None))
    csv_write(args.out / "runs.csv", rows)
    csv_write(args.out / "transport_summary.csv", summaries)
    csv_write(args.out / "goodput_100ms.csv", series)
    csv_write(args.out / "recovery.csv", recovery)
    failed = {p.parent.name: get(p) for p in sorted(args.raw.glob("*/failure_public.json"))}
    for path in sorted(args.raw.glob("*/failed.json")):
        failed.setdefault(path.parent.name, {"stage": "unsealed"})
    (args.out / "coverage.json").write_text(json.dumps(dict(success=len(rows), failed=failed), indent=2))
    frames_path = args.raw / "grace_frames.csv"
    if frames_path.exists():
        with frames_path.open() as handle:
            frames = list(csv.DictReader(handle))
        media = defaultdict(list)
        for frame in frames:
            media[frame["profile"], frame["algorithm"], int(frame["deadline_ms"])].append(frame)
        metrics = []
        for (profile, algorithm, deadline), values in sorted(media.items()):
            count = len(values)
            metrics.append(dict(profile=profile, algorithm=algorithm, deadline_ms=deadline, frames=count,
                runs=len({r["run"] for r in values}), clips=len({r["video"] for r in values}),
                on_time_block_pct=10*statistics.mean(int(r["blocks_on_time"]) for r in values),
                on_time_fragment_pct=100*sum(int(r["fragments_on_time"]) for r in values) /
                    sum(int(r["fragments_sent"]) for r in values),
                complete_frame_pct=100*sum(int(r["blocks_on_time"]) == 10 for r in values)/count,
                psnr_db=statistics.mean(float(r["psnr_db"]) for r in values),
                clean_psnr_db=statistics.mean(float(r["clean_psnr_db"]) for r in values)))
        csv_write(args.out / "grace_summary.csv", metrics)
        csv_write(args.out / "grace_frames.csv", frames)
    print(len(rows), "valid runs;", len(failed), "failed")


if __name__ == "__main__":
    main()
