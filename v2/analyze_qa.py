"""Reconcile paired questions, QoE, and endpoint/network traces."""

import argparse
import gzip
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analyze import csv_write, get, kernel_metrics, packet_metrics, packets, quantile


def millis(end, start):
    return (end-start)*1000 if end is not None and start is not None else None


def qevents(path):
    if not path.exists():
        return []
    with gzip.open(path, "rt") as handle:
        return [event for trace in json.load(handle)["traces"] for event in trace["events"]]


def budget(profile, switches, duration_ms, offset_ms):
    """Configured forward service budget, integrated over applied change times."""
    initial = profile.get("service_pattern", profile.get("rate", [profile.get("forward_mbps")]))[0]
    if initial is None:
        return None
    def integral(end):
        last = 0
        capacity = initial
        result = 0
        for _, _, committed, kind, rate in switches:
            if kind != "rate" or committed >= end:
                break
            result += (committed-last)*capacity
            last, capacity = committed, rate
        return result + (end-last)*capacity
    return (integral(offset_ms+duration_ms)-integral(offset_ms))/duration_ms


def peak_goodput(arrivals, start, end, width):
    from bisect import bisect_left
    times = [t for t, _ in arrivals]
    maximum = 0
    for left in [start, *times]:
        if left > end:
            break
        lo, hi = bisect_left(times, left), bisect_left(times, left+width)
        if hi:
            maximum = max(maximum, arrivals[hi-1][1] - (arrivals[lo-1][1] if lo else 0))
    return maximum*8/width/1e6


def high_slot_metrics(path, run, start, end, samples):
    profile = run["profile"]
    if "rate" not in profile or run["target"] is None:
        return {}
    target = run["target"]
    boundaries = [(target, profile.get("service_pattern", profile["rate"])[0])]
    boundaries.extend((target + event[2]/1000, event[4]) for event in run["switches"])
    intervals = [(max(a,start), min(b,end)) for (a, rate), (b, _) in
                 zip(boundaries, boundaries[1:]+[(end, 0)]) if rate==max(profile["rate"]) and b > start and a < end]
    seconds = sum(b-a for a,b in intervals)
    if seconds <= 0:
        return {}
    def is_high(stamp):
        return any(a <= stamp < b for a,b in intervals)
    tx = [(stamp+start,size) for stamp,direction,size,_ in
          packets(path / "tx.pcap.gz", start+run["wall_minus_mono"]) if direction=="forward"]
    tx_bytes = sum(size for stamp,size in tx if is_high(stamp))
    window = [s for s in samples if is_high(s[0])]
    return dict(high_slot_ms=seconds*1000, high_slot_tx_ip_mbps=tx_bytes*8/seconds/1e6,
                high_slot_cwnd_low_credit_pct=100*statistics.mean(s[6] for s in window) if window else None,
                high_slot_native_pacer_mbps=quantile([1200*8/1e6/s[4] for s in window if s[4]],.5))


def summarize_case(path, trace):
    work = path.parent
    run = get(path)
    client = get(work / "client.json") if (work / "client.json").exists() else {}
    server = get(work / "server.json") if (work / "server.json").exists() else {}
    start = client.get("start")
    first = client.get("first")
    finish = client.get("finish")
    upload_done = server.get("upload_done")
    available = run["status"] == "complete" and first is not None
    valid = available and client.get("prediction") is not None
    correct = valid and client.get("prediction") == run["label"]
    generated_first = next((stamp for stamp, token in server.get("tokens", []) if token.strip()), None)
    row = dict(case=run["case"], run=work.name, algorithm=run["algorithm"],
               question=run["question_id"], video=run["video"], sha256=run["sha256"],
               input_bytes=run["input_bytes"], status=run["status"], verified_upload=run["verified_upload"],
               visible_answer=bool(available), valid_answer=bool(valid), correct=bool(correct),
               prediction=client.get("prediction"), label=run["label"], handshake_ms=client.get("handshake_ms"),
               dispatch_offset_ms=millis(start, run["target"]),
               upload_ms=millis(upload_done, start), model_to_first_ms=millis(generated_first, server.get("infer_start")),
               model_total_ms=millis(server.get("infer_end"), server.get("infer_start")),
               downlink_first_ms=millis(first, generated_first),
               ttft_ms=millis(first, start) if available else None,
               ttlt_ms=millis(client.get("last"), start) if available else None,
               finish_ms=millis(finish, start) if available else None,
               dual_pcap=all((work/(side+".pcap.gz")).is_file() for side in ("tx","rx")),
               dual_qlog=all((work/(side+".qlog.json.gz")).is_file() for side in ("client","server")))
    for sla in (500, 1000, 2000, 3000):
        row[f"correct_within_{sla}ms"] = bool(correct and row["ttft_ms"] <= sla)
    row.update(kernel_metrics(dict(qdisc_before=run["qdisc_before"], qdisc_after=run["qdisc_after"],
                                   qdisc_samples=[], switches=run["switches"])))
    samples = [x for x in client.get("samples", []) if upload_done is None or x[0] <= upload_done]
    waits = [x for x in client.get("pacing_waits", []) if start and start <= x[0] <= (upload_done or finish)]
    wakes = [x for x in client.get("pacing_wakes", []) if start and start <= x[0] <= (upload_done or finish)]
    row.update(cwnd_first_bytes=samples[0][1] if samples else None,
               cwnd_p50_bytes=quantile([x[1] for x in samples], .5),
               cwnd_min_bytes=min((x[1] for x in samples), default=None),
               inflight_p50_bytes=quantile([x[2] for x in samples], .5),
               srtt_p50_ms=quantile([x[3]*1000 for x in samples if x[3]], .5),
               cwnd_low_credit_pct=100*statistics.mean(x[6] for x in samples) if samples else None,
               pacing_target_p50_mbps=quantile([1200*8/1e6/x[4] for x in samples if x[4]], .5),
               pacing_wait_checks=len(waits),
               pacing_wake_lag_p95_ms=quantile([(x[1]-x[0])*1000 for x in wakes], .95),
               cap_wait_checks=client.get("service_cap_waits"),
               pto_count_max=max((x[7] for x in samples), default=None))
    arrivals = server.get("arrivals", [])
    if start:
        end = upload_done or finish
        for width in (10, 50, 100, 200):
            delivered = max((n for t, n in arrivals if t <= start+width/1000), default=0)
            row[f"delivered_{width}ms_bytes"] = delivered
            row[f"upt_{width}ms_mbps"] = delivered*8/(width*1000)
            row[f"service_{width}ms_mbps"] = budget(run["profile"], run["switches"], width,
                                                     millis(start,run["target"])) if (
                "rate" not in run["profile"] or end >= start + width/1000) else None
            row[f"peak_upt_{width}ms_mbps"] = peak_goodput(arrivals, start, end, width/1000)
        if end and end > start and (work/"tx.pcap.gz").exists() and (work/"rx.pcap.gz").exists():
            row.update(packet_metrics(work, dict(start=start, wall_minus_mono=run["wall_minus_mono"],
                                                  duration=(end-start)+.002)))
            row.update(high_slot_metrics(work,run,start,end,samples))
        t0 = (start + run["wall_minus_mono"])*1000
        t1 = ((upload_done or finish) + run["wall_minus_mono"])*1000
        sender_events = qevents(work / "client.qlog.json.gz")
        row["quic_version"] = next((e["data"]["chosen_version"] for e in sender_events
                                    if e["name"]=="transport:version_information"),None)
        left = [e for e in sender_events if t0 <= e["time"] <= t1]
        lost = {e["data"]["packet_number"] for e in left if e["name"]=="recovery:packet_lost"
                and e["data"]["type"]=="1RTT"}
        peer_packets = [e for e in qevents(work / "server.qlog.json.gz")
                        if e["name"]=="transport:packet_received" and e["data"]["header"]["packet_type"]=="1RTT"]
        observed = {e["data"]["header"]["packet_number"] for e in peer_packets}
        max_seen = -1
        disorder = []
        for event in peer_packets:
            if t0 <= event["time"] <= t1 and any(f["frame_type"] == "stream" for f in event["data"]["frames"]):
                pn = event["data"]["header"]["packet_number"]
                if pn < max_seen:
                    disorder.append(max_seen-pn)
                max_seen = max(max_seen,pn)
        row["declared_lost_1rtt"] = len(lost)
        row["declared_lost_but_received"] = len(lost & observed)
        row["reordered_stream_packets"] = len(disorder)
        row["reorder_depth_p95"] = quantile(disorder,.95)
        row["stream_offsets_sent"] = sum(frame["frame_type"]=="stream" for event in left
            if event["name"]=="transport:packet_sent" for frame in event["data"].get("frames", []))
        row["flow_blocked_frames"] = sum(frame["frame_type"] in ("data_blocked","stream_data_blocked")
            for event in left if event["name"]=="transport:packet_sent" for frame in event["data"].get("frames", []))
        trace.write(json.dumps(dict(run=work.name,
            samples=[[round((s[0]-start)*1000,3), *s[1:8],
                      round((s[8]-start)*1000,3) if s[8] is not None else None] for s in samples],
            waits=[[round((s[0]-start)*1000,3),round((s[1]-start)*1000,3),s[2],s[3]] for s in waits],
            wakes=[[round((s[0]-start)*1000,3),round((s[1]-start)*1000,3)] for s in wakes],
            arrivals=[[round((stamp-start)*1000,3),n] for stamp,n in arrivals], switches=run["switches"]))+"\n")
    return row


def main():
    p = argparse.ArgumentParser()
    p.add_argument("raw", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--expected", type=int, default=12)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.out / "traffic_trace.jsonl.gz", "wt", compresslevel=6) as trace:
        rows = [summarize_case(path, trace) for path in sorted(args.raw.glob("*/run.json"))]
    groups = defaultdict(list)
    for row in rows:
        groups[row["case"]].append(row)
    summary = []
    for case, group in sorted(groups.items()):
        values = dict(case=case, n=len(group), complete=sum(x["status"]=="complete" for x in group),
                      verified_uploads=sum(x["verified_upload"] for x in group),
                      visible=sum(x["visible_answer"] for x in group), valid=sum(x["valid_answer"] for x in group),
                      correct=sum(x["correct"] for x in group))
        for metric in ("upload_ms","ttft_ms","ttlt_ms","model_to_first_ms","downlink_first_ms","finish_ms",
                       "handshake_ms","cwnd_p50_bytes","cwnd_low_credit_pct","pacing_target_p50_mbps",
                       "pacing_wake_lag_p95_ms","forward_qdisc_drops","declared_lost_1rtt",
                       "declared_lost_but_received","tx_nonempty_ms_burst_p95","transit_p95_ms",
                       "reordered_stream_packets","reorder_depth_p95",
                       "switch_commit_lag_p95_ms","upt_10ms_mbps","upt_50ms_mbps","upt_100ms_mbps",
                       "peak_upt_10ms_mbps","peak_upt_50ms_mbps","peak_upt_100ms_mbps",
                       "service_10ms_mbps","service_50ms_mbps","service_100ms_mbps",
                       "high_slot_tx_ip_mbps","high_slot_cwnd_low_credit_pct","high_slot_native_pacer_mbps"):
            series = [x[metric] for x in group if x.get(metric) is not None]
            values[metric+"_p50"] = quantile(series,.5)
            if metric in ("ttft_ms","upload_ms","finish_ms"):
                values[metric+"_p95"] = quantile(series,.95)
        for sla in (500,1000,2000,3000):
            values[f"correct_within_{sla}ms"] = sum(x[f"correct_within_{sla}ms"] for x in group)
        summary.append(values)
    pairs = []
    comparisons = [("steady", "steady_cap8"), ("steady", "steady_reno"),
                   ("steady", "delay10"), ("steady", "delay50"),
                   ("rate10_low", "rate10_high"), ("rate50_low", "rate50_high"),
                   ("rate100_low", "rate100_high"), ("rate50_high","rate50_matched"),
                   ("rate50_opposite","rate50_matched"), ("rate50_high","rate50_reno"),
                   ("grant_inverse","grant_replay"), ("steady","grant_replay"),
                   ("delay10","delay10_fifo"), ("steady","delay10_fifo"),
                   ("steady","delay_slot30"), ("delay10","delay_slot30")]
    for reference, candidate in comparisons:
        by_id = {row["question"]:row for row in groups.get(reference, [])}
        for row in groups.get(candidate, []):
            if prior := by_id.get(row["question"]):
                assert row["sha256"] == prior["sha256"]
                pairs.append(dict(reference=reference, candidate=candidate, question=row["question"],
                    video=row["video"], reference_complete=prior["status"]=="complete",
                    candidate_complete=row["status"]=="complete",
                    upload_delta_ms=row["upload_ms"]-prior["upload_ms"]
                    if row["upload_ms"] is not None and prior["upload_ms"] is not None else None,
                    ttft_delta_ms=row["ttft_ms"]-prior["ttft_ms"]
                    if row["ttft_ms"] is not None and prior["ttft_ms"] is not None else None))
    csv_write(args.out/"requests.csv",rows)
    csv_write(args.out/"profiles.csv",summary)
    csv_write(args.out/"paired.csv",pairs)
    env = get(args.raw/"environment.json")
    check = dict(cases=len(groups), requests=len(rows), expected_per_case=args.expected,
        status=dict(Counter(r["status"] for r in rows)), input_sizes=sorted({r["input_bytes"] for r in rows}),
        verified_uploads=sum(r["verified_upload"] for r in rows),
        dual_pcap=sum(r["dual_pcap"] for r in rows), dual_qlog=sum(r["dual_qlog"] for r in rows),
        quic_versions=sorted({r["quic_version"] for r in rows if r.get("quic_version") is not None}))
    (args.out/"validation.json").write_text(json.dumps(check,indent=2)+"\n")
    assert set(groups)==set(env["cases"]) and all(len(group)==args.expected for group in groups.values()), check
    assert all(len({r["question"] for r in group})==args.expected for group in groups.values())
    assert all(r["dual_pcap"] and r["dual_qlog"] for r in rows)
    assert all(r["verified_upload"] for r in rows if r["status"]=="complete")
    assert all(r["ttft_ms"] is None or 0 < r["ttft_ms"] <= r["finish_ms"] for r in rows)
    assert all(r[key] is None or r[key] >= 0 for r in rows
               for key in ("upload_ms","model_to_first_ms","downlink_first_ms"))
    assert all(len({r["sha256"] for r in rows if r["question"]==question})==1
               for question in {r["question"] for r in rows})
    with sqlite3.connect(args.raw/"traffic.sqlite") as db:
        sessions = db.execute("SELECT session_id,success FROM traffic_logs").fetchall()
    assert Counter(sessions)==Counter((r["run"],int(r["status"]=="complete")) for r in rows)
    print(check)


if __name__ == "__main__":
    main()
