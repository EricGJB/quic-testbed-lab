"""Extract matched loss decisions and dwell-phase delivery from local raw runs."""

import argparse
import gzip
import json
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze import bulk_metrics, csv_write, get, kernel_metrics, packets, transport_metrics


def summarize(rows, keys):
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row[k] for k in keys)].append(row)
    return [dict(zip(keys, group), runs=len(members), **{
        k: round(median(row[k] for row in members), 5)
        for k in members[0] if k not in (*keys, "run")
    }) for group, members in sorted(groups.items())]


def receiver_packets(path):
    with gzip.open(path / "server.qlog.json.gz", "rt") as stream:
        qlog = json.load(stream)
    return {event["data"]["header"]["packet_number"]: event["time"]
            for trace in qlog["traces"] for event in trace["events"]
            if event["name"] == "transport:packet_received"
            and event["data"]["header"]["packet_type"] == "1RTT"}


def loss_rows(root):
    rows, events = [], []
    for path in sorted(root.glob("bulk__*/run.json")):
        run = get(path)
        path = path.parent
        start, wall = run["start"], run["wall_minus_mono"]
        seen = receiver_packets(path)
        counts = Counter()
        for timestamp, before, after, rtt, decisions in get(path / "loss_decisions.json"):
            if not start <= timestamp < start + run["duration"]:
                continue
            counts["cwnd_cuts"] += after < before
            for pn, epoch, reason, gap, age, in_flight in decisions:
                if epoch != "ONE_RTT":
                    continue
                arrival_ms = seen.get(pn)
                counts["declared_lost"] += 1
                if arrival_ms is not None:
                    counts["original_received"] += 1
                    counts["received_" + reason] += 1
                    counts["original_received_after_declaration"] += arrival_ms > (timestamp + wall)*1000
                events.append(dict(run=path.name, packet_number=pn, trigger=reason,
                    ack_gap_packets=gap, packet_age_ms=round(age*1000, 4),
                    rtt_reference_ms=round(rtt*1000, 4),
                    declared_ms=round((timestamp-start)*1000, 4),
                    peer_arrival_ms=round(arrival_ms-(start+wall)*1000, 4) if arrival_ms is not None else "",
                    cwnd_before_bytes=before, cwnd_after_bytes=after, in_flight=int(in_flight)))
        transport = transport_metrics(path, run)
        if counts["declared_lost"] != transport["declared_lost_1rtt"]:
            raise ValueError(f"probe and qlog loss totals differ for {path.name}")
        if counts["original_received"] != transport["declared_lost_but_received"]:
            raise ValueError(f"probe and qlog receive matches differ for {path.name}")
        rows.append(dict(run=path.name, algorithm=run["algorithm"], profile=run["profile"],
            goodput_mbps=round(bulk_metrics(path, run)[0]["goodput_mbps"], 5),
            forward_qdisc_drops=kernel_metrics(run)["forward_qdisc_drops"],
            cwnd_bytes_p50=transport["cwnd_bytes_p50"], srtt_ms_p50=transport["srtt_p50_ms"],
            pacing_mbps_p50=transport["pacer_target_mbps_p50"], **{key: counts[key] for key in (
                "declared_lost", "original_received", "original_received_after_declaration",
                "received_number", "received_time", "received_both", "received_rescheduled", "cwnd_cuts")}))
    return rows, events


def phase_rows(root):
    rows = []
    for path in sorted(root.glob("bulk__*__rate*__*/run.json")):
        run = get(path)
        path = path.parent
        if run["profile"] not in {"rate10ms", "rate20ms", "rate100ms"}:
            continue
        start = run["start"]
        switches = [row[2]/1000 for row in run["switches"]]
        received = get(path / "received.json")["received"]
        app_times = [stamp-start for stamp, _ in received]

        def delivered(t):
            index = bisect_right(app_times, t)
            return received[index-1][1] if index else 0

        def slot_bytes(capture):
            totals = Counter()
            for timestamp, direction, size, _ in packets(path / capture, start + run["wall_minus_mono"]):
                if direction == "forward" and switches[0] <= timestamp < switches[-1]:
                    totals[bisect_right(switches, timestamp)-1] += size
            return totals

        tx = slot_bytes("tx.pcap.gz")
        rx = slot_bytes("rx.pcap.gz")

        for rate in (5, 40):
            slots = [i for i, row in enumerate(run["switches"][:-1]) if row[4] == rate]
            duration = sum(switches[i+1]-switches[i] for i in slots)
            app_bytes = sum(delivered(switches[i+1])-delivered(switches[i]) for i in slots)
            rows.append(dict(run=path.name, algorithm=run["algorithm"], profile=run["profile"],
                configured_mbps=rate, slots=len(slots), seconds=round(duration, 6),
                sender_ip_mbps=round(sum(tx[i] for i in slots)*8/duration/1e6, 5),
                receiver_ip_mbps=round(sum(rx[i] for i in slots)*8/duration/1e6, 5),
                app_mbps=round(app_bytes*8/duration/1e6, 5)))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loss", type=Path, required=True)
    parser.add_argument("--phase", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    loss, events = loss_rows(args.loss)
    phase = phase_rows(args.phase)
    if not loss or not phase:
        parser.error("no matching raw runs found")
    args.out.mkdir(parents=True, exist_ok=True)
    for name, rows in (
        ("loss-runs", loss), ("loss-events", events),
        ("loss-summary", summarize(loss, ("algorithm", "profile"))),
        ("phase-runs", phase), ("phase-summary", summarize(phase, ("algorithm", "profile", "configured_mbps"))),
    ):
        csv_write(args.out / (name + ".csv"), rows)


if __name__ == "__main__":
    main()
