"""Check the planned denominators, provenance and traces before release."""

import argparse
import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path

import yaml


def check(condition, message):
    if not condition:
        raise ValueError(message)


def verify(root, reports, definition, repeats, selected=None):
    catalog = yaml.safe_load(definition.read_text())
    cases = [(mode, algorithm, profile) for mode, algorithms in catalog["matrix"].items()
             for algorithm, profiles in algorithms.items() for profile in profiles]
    if selected:
        cases = [case for case in cases if case in selected]
    expected = {f"{mode}__{algo}__{profile}__{index:02d}"
                for mode, algo, profile in cases for index in range(repeats)}
    records = {p.parent.name: p.parent for p in root.glob("*/run.json")}
    failures = {p.parent.name: p for p in root.glob("*/failure_public.json")}
    observed = set(records) | set(failures)
    check(expected == observed and not set(records) & set(failures),
          f"missing {expected - observed}, extra {observed - expected}")
    check({p.parent.name for p in root.glob("*/failed.json")} == set(failures), "unsealed failure")
    with sqlite3.connect(f"file:{root / 'traffic.sqlite'}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT session_id, success FROM traffic_logs").fetchall()
    check(Counter(rows) == Counter((session, int(session in records)) for session in expected),
          "Testbed DB denominator")
    trace_counts = Counter()
    for name, path in failures.items():
        failure = json.loads(path.read_text())
        check(failure["workload"] == "grace" and failure["profile"] == "congested"
              and failure["stage"] == "startup_timeout" and failure["receiver_started"]
              and not failure["workload_started"], "unexpected failure: " + name)
        for side in ("tx", "rx"):
            check((path.parent / (side + ".pcap.gz")).stat().st_size > 24, f"missing failure PCAP: {name}")
            check("0 packets dropped by kernel" in (path.parent / (side + ".capture.txt")).read_text(), name)
        trace_counts["pcap_pairs"] += 1
    for name, path in records.items():
        run = json.loads((path / "run.json").read_text())
        profile = catalog["scenarios"][run["profile"]]
        if "rate_step" in profile or "delay_step" in profile:
            count = len(range(profile["begin_ms"], profile["end_ms"], profile["dwell_ms"])) + 1
            check(count == len(run["switches"]), f"incomplete switching schedule: {name}")
            check(all(planned <= began <= committed for planned, began, committed, *_ in run["switches"]),
                  f"noncausal switch: {name}")
        for side in ("tx", "rx"):
            check((path / (side + ".pcap.gz")).stat().st_size > 24, f"missing PCAP: {name}")
            check("0 packets dropped by kernel" in (path / (side + ".capture.txt")).read_text(), name)
        has_qlog = (path / "client.qlog.json.gz").exists() and (path / "server.qlog.json.gz").exists()
        check(has_qlog == (run["algorithm"] != "tcp" and selected is None), f"qlog coverage: {name}")
        trace_counts["qlog_pairs"] += has_qlog
        trace_counts["pcap_pairs"] += 1
        received = json.loads((path / "received.json").read_text())
        if run["workload"] == "bulk":
            check(not received["corrupt_bytes"] and received["total"] > 0, name)
        else:
            check(received["iframe_verified"] and len(received["frames"]) == 42, name)
    with (reports / "runs.csv").open(encoding="utf-8") as handle:
        summarized = list(csv.DictReader(handle))
    check(Counter(r["run"] for r in summarized) == Counter(records.keys()), "summary denominator")
    grace_runs = {name for name in records if name.startswith("grace__")}
    if grace_runs:
        with (reports / "grace_frames.csv").open(encoding="utf-8") as handle:
            frames = list(csv.DictReader(handle))
        counts = Counter((r["run"], int(r["deadline_ms"])) for r in frames)
        check(counts == Counter({(run, d): 42 for run in grace_runs for d in (50, 100, 200)}), "GRACE denominator")
        check(all(0 <= int(r["blocks_on_time"]) <= 10 and
                  0 <= int(r["fragments_on_time"]) <= int(r["fragments_sent"]) for r in frames),
              "invalid deadline counts")
        check(all(float(r["psnr_db"]) > 0 for r in frames), "invalid PSNR")
    return dict(cases=len(cases), runs=len(expected), successful=len(records),
                bulk_runs=len(records)-len(grace_runs), grace_completed=len(grace_runs),
                grace_startup_timeouts=len(failures), **trace_counts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("raw", type=Path)
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    config = Path(__file__).resolve().parent
    report = dict(
        primary=verify(args.raw, args.results, config / "profiles.yaml", 3),
        followup=verify(args.raw / "followup", args.results / "followup", config / "followup.yaml", 3),
        no_qlog=verify(args.raw / "no-qlog", args.results / "no-qlog", config / "profiles.yaml", 3,
                       {("bulk", "cubic", p) for p in ("fixed40", "rate10ms")}),
    )
    text = json.dumps(report, indent=2)
    (args.results / "validation.json").write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
