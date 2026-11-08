"""Verify per-stream delivery and derive the four case studies from raw traces."""

import argparse
import csv
import gzip
import io
import json
import re
import sys
import tarfile
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze import csv_write, get, quantile


class Timeline:
    def __init__(self, rows, field="us"):
        self.rows = rows
        self.times = [int(row[field]) for row in rows]
        if self.times != sorted(self.times):
            raise ValueError("nonmonotonic trace")

    def between(self, left, right):
        return self.rows[bisect_left(self.times,left):bisect_left(self.times,right)]

    def prior(self, when):
        index = bisect_right(self.times, when)-1
        return self.rows[index] if index >= 0 else None


def read(path, text_fields):
    with path.open() as handle:
        return [{key: value if key in text_fields else int(value) for key,value in row.items()}
                for row in csv.DictReader(handle)]


def stats(data):
    return [quantile(data, q) for q in (.05, .5, .95, .99)]


def inspect(work, fixtures):
    plan = get(work/"plan.json")
    case, origin, repeat = [plan[key] for key in ("case","origin","repeat")]
    client = {(row["task"],row["event"]):row for row in read(work/"client.csv", {"event"})}
    server = {row["task"]:row for row in read(work/"server.csv", {"event"})}
    samples = Timeline(read(work/"samples.csv", set()))
    if not samples.times or samples.times[-1] < origin + plan["duration"] - 5000:
        raise ValueError(f"incomplete endpoint trace in {work.name}")
    wire = Timeline(read(work/"wire.csv", {"event"}))
    offsets, rates = zip(*plan["capacity"])
    switch_times = [origin+t for t in offsets]

    def capacity(at):
        return rates[max(0,bisect_right(switch_times,at)-1)]

    def service_bytes(left, right):
        cuts = [left,*switch_times[bisect_right(switch_times,left):bisect_left(switch_times,right)],right]
        return sum(capacity(a)*(b-a)/8 for a,b in zip(cuts,cuts[1:]))

    def window(lo, hi):
        packets = wire.between(lo,hi)
        prior = wire.prior(lo-1)
        return dict(tx=sum(row["bytes"] for row in packets if row["event"] == "in"),
                    rx=sum(row["bytes"] for row in packets if row["event"] == "out"),
                    queue=max([row["queue"] for row in packets] + [prior["queue"] if prior else 0]))

    rows, curves = [], []
    schedule_lateness = []
    for index, task in enumerate(plan["tasks"]):
        start, token, end = client.get((index,"start")), client.get((index,"token")), server.get(index)
        if end:
            expected = fixtures[task["question"]][task["tier"]]["wire_hash"] if "question" in task else 0
            if end["bytes"] != task["size"] or end["hash"] != expected or not start:
                raise ValueError(f"content mismatch in {work.name} task {index}")
        if token and not end:
            raise ValueError("response without upload")
        if not start:
            if not task["retry"] or (index,"skip") not in client:
                raise ValueError(f"missing task launch {work.name} {index}")
            continue
        if task.get("warmup"):
            continue
        lo = start["us"]
        schedule_lateness.append(lo-origin-task["t"])
        budget = task["budget"]
        relaxed = task.get("relaxed",budget)
        completion = end["us"]-lo if end else None
        response = token["us"]-lo if token else None
        primary = window(lo,lo+budget)
        active = samples.between(lo,lo+budget)
        pacing_gap = [abs(row["pacing_bps"]/1e6-capacity(row["us"])) for row in active]
        tolerance = [gap <= .1*capacity(row["us"]) for gap,row in zip(pacing_gap,active)]
        converge = next(((r["us"]-lo)/1000 for i,r in enumerate(active[:-2])
                         if all(tolerance[i:i+3])), None)
        rows.append(dict(case=case, repeat=repeat, task=index, size=task["size"],
            planned_ms=task["t"]/1000, start_ms=(lo-origin)/1000,
            upload_ms=completion/1000 if completion is not None else None,
            token_ms=response/1000 if response is not None else None,
            on_time=int(completion is not None and completion <= budget),
            token_on_time=int(response is not None and response <= budget),
            relaxed_ms=relaxed/1000, relaxed_on_time=int(completion is not None and completion <= relaxed),
            cap_mbps=capacity(lo), pacing_initial_mbps=active[0]["pacing_bps"]/1e6 if active else None,
            cwnd_initial_bytes=active[0]["cwnd"] if active else None,
            mismatch_mbps=median(pacing_gap) if pacing_gap else None, match3_ms=converge,
            initial_rtt_ms=active[0]["srtt_us"]/1000 if active else None,
            cwnd_jump_bytes=(max(r["cwnd"] for r in active)-min(r["cwnd"] for r in active)) if active else None,
            pace_span_mbps=((max(r["pacing_bps"] for r in active)-min(r["pacing_bps"] for r in active))/1e6)
                if active else None,
            queue20_bytes=primary["queue"], tx20_bytes=primary["tx"], rx20_bytes=primary["rx"],
            reserved_bytes=round(service_bytes(origin+task["t"],origin+task["t"]+relaxed)),
            question=task.get("question"), tier=task.get("tier"), predicted=task.get("predicted"),
            logical=task.get("logical"), retry=int(bool(task["retry"]))))
        if case.startswith(("B","D")):
            for offset in range(0, 40_000, 1000):
                phase = window(lo+offset,lo+offset+1000)
                point = samples.prior(lo+offset)
                if point:
                    curves.append(dict(case=case, repeat=repeat, task=index, size=task["size"],
                        ms=offset/1000, cap_mbps=capacity(lo+offset), pacing_mbps=point["pacing_bps"]/1e6,
                        cwnd_bytes=point["cwnd"], **phase))

    plateau = samples.between(origin+3_000_000,origin+6_000_000)
    active = [r for r in plateau if r["inflight"] and r["pacing_bps"]]
    plate = window(origin+3_000_000,origin+6_000_000)
    drop_count = sum(r["event"] == "drop" for r in wire.rows)
    relay_slip = [max(0,r["us"]-r["due_us"])/1000 for r in wire.rows if r["event"] == "out"]
    runs = dict(case=case, repeat=repeat, tasks=len(rows), uploaded=sum(r["upload_ms"] is not None for r in rows),
                drops=drop_count, release_late_p99_ms=quantile(relay_slip,.99),
                dispatch_late_p99_ms=quantile(schedule_lateness,.99)/1000,
                retransmissions=samples.rows[-1]["lost"])
    long_trace = []
    if case.startswith("A"):
        runs.update(active_samples=len(active), capacity_mbps=service_bytes(origin+3_000_000,origin+6_000_000)*8/3e6,
                    receiver_ip_mbps=plate["rx"]*8/3e6, sender_ip_mbps=plate["tx"]*8/3e6,
                    pace=stats([r["pacing_bps"]/1e6 for r in active]),
                    bw=stats([r["btlbw_bps"]/1e6 for r in active]),
                    cwnd=stats([r["cwnd"]/1000 for r in active]),
                    queue=stats([r["queue"]/1000 for r in wire.between(origin+3_000_000,origin+6_000_000)]),
                    mismatch=stats([abs(r["pacing_bps"]/1e6-capacity(r["us"])) for r in active]),
                    overshoot_pct=100*sum(r["pacing_bps"]/1e6>capacity(r["us"])+.1 for r in active)/len(active),
                    states=dict(Counter(r["state"] for r in active)))
        for point in range(600_000, 6_500_000, 10_000):
            t = origin+point
            data = samples.between(t,t+10000)
            if data:
                sample = data[len(data)//2]
                long_trace.append(dict(case=case, repeat=repeat, time_ms=point/1000,
                    cap_mbps=capacity(t), pacing_mbps=sample["pacing_bps"]/1e6,
                    btlbw_mbps=sample["btlbw_bps"]/1e6, cwnd_kb=sample["cwnd"]/1000,
                    queue_kb=window(t,t+10000)["queue"]/1000))
    return rows, runs, curves, long_trace


def model_summary(fixture_dir):
    rows = []
    for entry in get(fixture_dir/"model.json"):
        text = "".join(s for _,s in entry["tokens"]).strip()
        match = re.fullmatch(r"([ABCD])[.)。]?",text,flags=re.I)
        prediction = match.group(1).upper() if match else ""
        first = next((t for t,s in entry["tokens"] if s.strip()),None)
        if first is None:
            raise ValueError("model produced no visible text")
        rows.append(dict(question=entry["question"],tier=entry["tier"],label=entry["label"],
            prediction=prediction,correct=int(prediction == entry["label"]),
            first_ms=round((first-entry["infer_start"])*1000,3)))
    return rows


def summary(tasks, model):
    groups = defaultdict(list)
    for row in tasks:
        if row["retry"]:
            continue
        name = row["case"]
        group = str(row["size"]) if name.startswith(("B","D")) else str(row["cap_mbps"]) if name.startswith("C") else "all"
        groups[name,group].append(row)
    lookup = {(r["question"],r["tier"]):r for r in model}
    retries = {(r["case"],r["repeat"],r["logical"]):r for r in tasks if r["retry"]}
    report = []
    for (case,group), rows in sorted(groups.items()):
        def col(name):
            return [r[name] for r in rows if r[name] is not None]
        out = dict(case=case, group=group, tasks=len(rows), uploaded=len(col("upload_ms")),
            on_time=sum(col("on_time")), response20=sum(col("token_on_time")), relaxed_on_time=sum(col("relaxed_on_time")),
            upload_p50=quantile(col("upload_ms"),.5), upload_p95=quantile(col("upload_ms"),.95),
            upload_p99=quantile(col("upload_ms"),.99), token_p99=quantile(col("token_ms"),.99),
            queue20_p50=quantile(col("queue20_bytes"),.5), queue20_p95=quantile(col("queue20_bytes"),.95),
            cwnd_initial_p50=quantile(col("cwnd_initial_bytes"),.5), cwnd_span_p50=quantile(col("cwnd_jump_bytes"),.5),
            pace_initial_p50=quantile(col("pacing_initial_mbps"),.5), pace_span_p50=quantile(col("pace_span_mbps"),.5),
            gap_p50=quantile(col("mismatch_mbps"),.5), matched20=len(col("match3_ms")),
            match_p50=quantile(col("match3_ms"),.5),
            reserved_fill_pct=100*sum(r["size"] for r in rows if r["relaxed_on_time"])/sum(col("reserved_bytes")))
        if case.startswith("C"):
            chosen = [lookup[r["question"],r["tier"]] for r in rows]
            out.update(low=sum(r["tier"]=="low" for r in rows),
                       retries=sum((case,r["repeat"],r["logical"]) in retries for r in rows),
                       retried_ok=sum(bool(retries.get((case,r["repeat"],r["logical"]), {}).get("on_time"))
                                      for r in rows),
                       correct=sum(r["correct"] for r in chosen),
                       correct20=sum(row["on_time"]*r["correct"] for row,r in zip(rows,chosen)))
        report.append(out)
    return report


def archive(path, raw):
    chosen = {"A1load_random", "A1load_gold", "A1load_slow", "B1_rtt_elastic", "B1_rtt_fast",
              "C1_reactive", "C1_oracle", "D1_high", "D1_pattern"}
    with tarfile.open(path,"w:xz",preset=3) as tar:
        def add(name, data):
            header = tarfile.TarInfo(name)
            header.size = len(data)
            tar.addfile(header,io.BytesIO(data))
        for work in sorted(raw,key=lambda d:d.name):
            if not work.name.endswith("__00") or work.name.split("__")[0] not in chosen:
                continue
            plan = get(work/"plan.json")
            for task in plan["tasks"]:
                task["file"] = Path(task["file"]).name
            add(f"{work.name}/plan.json",json.dumps(plan,separators=(",",":")).encode())
            names = ["client.csv","server.csv","samples.csv","wire.csv"]
            names.extend(str(p.relative_to(work)) for role in ("client","server")
                         for p in sorted((work/f"qlog-{role}").glob("*.qlog")))
            for name in names:
                add(f"{work.name}/{name}",(work/name).read_bytes())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("raw",type=Path)
    p.add_argument("fixtures",type=Path)
    p.add_argument("out",type=Path)
    p.add_argument("--repeat-zero",type=Path,help="clean rerun that replaces all replicate-0 cases")
    p.add_argument("--extra",type=Path,nargs="*",default=[],help="additional case directories")
    p.add_argument("--archive",action="store_true")
    args = p.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    fixtures = get(args.fixtures/"fixtures.json")
    model = model_summary(args.fixtures)
    raw = [p.parent for p in sorted(args.raw.glob("*/plan.json"))]
    env = get(args.raw/"environment.json")
    expected = Counter({case:env["repeats"] for case in env["cases"]})
    if args.repeat_zero:
        replacement = get(args.repeat_zero/"environment.json")
        assert set(replacement["cases"]) == set(env["cases"]) and replacement["repeats"] == 1
        assert replacement["endpoint_hash"] == env["endpoint_hash"]
        raw = [p for p in raw if not p.name.endswith("__00")] + [p.parent for p in args.repeat_zero.glob("*/plan.json")]
        env["repeat_zero_replaced"] = True
    for path in args.extra:
        extra = get(path/"environment.json")
        assert not set(extra["cases"]).intersection(expected)
        assert extra["endpoint_hash"] == env["endpoint_hash"]
        raw.extend(p.parent for p in path.glob("*/plan.json"))
        expected.update({case:extra["repeats"] for case in extra["cases"]})
        env.setdefault("extra_cases",[]).extend(extra["cases"])
    tasks, runs, curves, long_trace = [], [], [], []
    for path in raw:
        case_tasks, case_run, curve, long = inspect(path, fixtures)
        tasks.extend(case_tasks)
        runs.append(case_run)
        curves.extend(curve)
        long_trace.extend(long)
    assert Counter(r["case"] for r in runs) == expected
    csv_write(args.out/"task-summary.csv",summary(tasks,model))
    csv_write(args.out/"model-results.csv",model)
    csv_write(args.out/"A1-trace.csv",long_trace)
    for name, data in (("tasks",tasks),("BD-curves",curves)):
        with gzip.open(args.out/(name+".csv.gz"),"wt",newline="") as handle:
            columns = list(dict.fromkeys(k for row in data for k in row))
            writer = csv.DictWriter(handle,fieldnames=columns,lineterminator="\n")
            writer.writeheader()
            writer.writerows(data)
    clean = {key:{**{tier:{k:v for k,v in entry[tier].items() if k!="path"}
                       for tier in ("low","high")},"label":entry["label"]} for key,entry in fixtures.items()}
    for filename,data in (("runs",runs),("fixtures",clean),("environment",env)):
        (args.out/(filename+".json")).write_text(json.dumps(data,indent=2)+"\n")
    if args.archive:
        archive(args.out/"trace-evidence.tar.xz",raw)
    print(f"verified {len(runs)} runs and {len(tasks)} launched tasks")


if __name__ == "__main__":
    main()
