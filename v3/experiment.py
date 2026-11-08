"""Run A1–D1 on a 6G-Testbed veth path with a scheduled service relay."""

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from run import TESTBED_COMMIT, cmd, dump, in_namespace, sha, stop_process, topology

CASES = (
    "A1_random", "A1_equal20", "A1_gold", "A1load_random", "A1load_equal20", "A1load_gold",
    "B1_fixed", "B1_variable", "B1_elastic", "B1_fast",
    "C1_reactive", "C1_oracle", "C1_80pct",
    "D1_native", "D1_high", "D1_low", "D1_equal", "D1_pattern",
)
EXTRA_CASES = ("A1load_slow", "B1_rtt_elastic", "B1_rtt_fast")
WARM = 600_000


def scenario(case, seed, fixtures):
    rng = random.Random(seed)
    tasks, switches = [], {0: 30}

    def task(t, size, budget, rate1=0, rate2=0, step=0, hold=0, window=0, **extra):
        tasks.append(dict(t=t, size=size, budget=budget, rate1=rate1, rate2=rate2, step=step,
                          hold=hold, window=window, retry=0, file="-", **extra))

    if case.startswith("A"):
        gap = 10_000 if "load" in case else 20_000
        n = 600 if "load" in case else 400
        for i in range(n):
            task(WARM + i*gap, 40_000, 20_000)
        stop = WARM + n*gap
        duration = stop + (7_000_000 if "load" in case else 500_000)
        if case.endswith("equal20"):
            switches[0] = 20
        else:
            high, low = (28, 30) if case.endswith("gold") else (30, 10)
            t = 0
            while t < duration:
                dwell = rng.randint(120_000, 180_000) if case.endswith("slow") else rng.randint(5000, 10000)
                switches[t] = high
                switches[t+dwell] = low
                t += 2*dwell
    elif case.startswith("B"):
        order = [40_000, 200_000, 1_000_000]*5
        rng.shuffle(order)
        for i, size in enumerate(order):
            t = WARM + i*650_000
            grant = 18 if size == 40_000 else 30
            relaxed = {40_000: 24_000, 200_000: 70_000, 1_000_000: 310_000}[size]
            adaptive = case.endswith(("elastic", "fast"))
            if case == "B1_variable":
                for j in range(0, 650_000, 10_000):
                    switches[t+j] = 30 if j % 20_000 == 0 else 10
            else:
                switches[t] = grant if adaptive else 20
                if adaptive:
                    switches[t+relaxed] = 20
            rate = grant*125_000 if case.endswith("fast") else 0
            window = rate * (14 if "_rtt_" in case else 4)//1000+3000 if rate else 0
            task(t, size, 20_000, rate, rate, hold=relaxed, window=window,
                 relaxed=relaxed, grant=grant)
        switches[0] = 20
        duration = WARM + len(order)*650_000
    elif case.startswith("C"):
        if not fixtures:
            raise ValueError("C1 requires actual JPEG fixtures")
        ids = sorted(fixtures)
        order = [(key, cap) for key in ids for cap in (10, 10, 30, 30)]
        rng.shuffle(order)
        errors = set(rng.sample(range(len(order)), round(.2*len(order))))
        for i, (key, cap) in enumerate(order):
            t = WARM + i*80_000
            switches[t] = cap
            predicted = (40-cap) if case == "C1_80pct" and i in errors else cap
            tier = "low" if case != "C1_reactive" and predicted == 10 else "high"
            task(t, fixtures[key][tier]["bytes"], 20_000, question=key, tier=tier, cap=cap,
                 predicted=predicted if case != "C1_reactive" else 0, logical=i)
            tasks[-1]["file"] = fixtures[key][tier]["path"]
            if case == "C1_reactive":
                initial = len(tasks)
                task(t+40_000, fixtures[key]["low"]["bytes"], 20_000, question=key, tier="low",
                     cap=cap, predicted=0, logical=i)
                tasks[-1].update(retry=initial, file=fixtures[key]["low"]["path"])
        duration = WARM + len(order)*80_000 + 150_000
    else:
        # Prime the connection before the measured tasks. The native window stays active.
        task(150_000, 350_000, 200_000, warmup=True)
        pattern = {"D1_native": (0,0), "D1_high": (28,28), "D1_low": (14,14),
                   "D1_equal": (21,21), "D1_pattern": (28,14)}[case]
        order = [40_000, 50_000]*24
        rng.shuffle(order)
        for i, size in enumerate(order):
            t = WARM + i*80_000
            switches.update({t: 30, t+10_000: 15, t+50_000: 30})
            task(t, size, 20_000, *(x*125_000 for x in pattern), step=10_000, hold=50_000)
        duration = WARM + len(order)*80_000 + 100_000
    return dict(tasks=tasks, capacity=sorted(switches.items()), duration=duration,
                delay_ms=6 if "_rtt_" in case else 1)


def launch(namespace, argv, log):
    with log.open("w") as handle:
        return subprocess.Popen(["ip", "netns", "exec", namespace, *map(str, argv)],
                                stdout=handle, stderr=subprocess.STDOUT)


def run_case(out, case, repeat, plan, namespaces, executable, emulator, logger, qlog):
    from analysis.logger import LogRecord
    work = out / f"{case}__{repeat:02d}"
    work.mkdir()
    client, router, server = namespaces
    with in_namespace(router):
        for device in ("r0", "r1"):
            shaper = emulator(interface=device, profiles_path=str(out / "profiles.yaml"), bidirectional=False)
            name = "propagation_long" if plan["delay_ms"] == 6 else "propagation"
            if not shaper.apply_profile(name):
                raise RuntimeError("cannot set propagation profile")
    origin = time.monotonic_ns()//1000 + 350_000
    duration = plan["duration"]
    mode = "qlog" if qlog else "plain"
    if qlog:
        for role in ("client", "server"):
            (work / f"qlog-{role}").mkdir()
    plan.update(origin=origin, case=case, repeat=repeat)
    dump(work / "plan.json", plan)
    with (work / "tasks.tsv").open("w") as stream:
        for task in plan["tasks"]:
            stream.write("\t".join(str(task[k]) for k in (
                "t", "size", "rate1", "rate2", "step", "hold", "window", "retry", "file")) + "\n")
    processes = []
    try:
        processes.append(launch(router, (sys.executable, Path(__file__).with_name("relay.py"),
            work, origin, duration), work / "relay.log"))
        for role, namespace in (("server", server), ("client", client)):
            processes.append(launch(namespace, (executable, role, work, origin, duration, mode),
                                    work / (role+".log")))
        for process in processes:
            if process.wait(timeout=duration/1e6+4):
                raise RuntimeError(f"{work}: a process exited {process.returncode}")
    finally:
        for process in processes:
            stop_process(process, signal.SIGTERM)
    logger.log(LogRecord(timestamp=time.time(), scenario_id="gold_pipe_v3", session_id=work.name,
        turn_index=0, network_profile=case, provider="picoquic-bbr1", model="burst-test",
        request_bytes=sum(task["size"] for task in plan["tasks"]), success=True))
    print(work.name, flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--testbed", type=Path, required=True)
    p.add_argument("--endpoint", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--fixtures", type=Path)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--only", nargs="+", choices=CASES+EXTRA_CASES, default=CASES)
    p.add_argument("--no-qlog", action="store_true")
    args = p.parse_args()
    if os.geteuid():
        p.error("requires root to configure isolated namespaces")
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    testbed = args.testbed.resolve()
    sys.path[:0] = [str(testbed / "netemu/src"), str(testbed / "aitestbed")]
    from netemu import NetworkEmulator
    from analysis.logger import TrafficLogger
    files = ("netemu/src/netemu/emulator.py", "netemu/src/netemu/profile.py", "aitestbed/analysis/logger.py")
    for name in files:
        blob = subprocess.check_output(["git", "-c", "safe.directory=" + str(testbed),
            "-C", str(testbed), "show", f"{TESTBED_COMMIT}:{name}"])
        if (testbed / name).read_bytes().replace(b"\r\n", b"\n") != blob:
            raise RuntimeError(f"unexpected Testbed component {name}")
    fixtures = json.loads(args.fixtures.read_text()) if args.fixtures else None
    (args.out / "profiles.yaml").write_text("profiles:\n  propagation: {delay_ms: 1, limit_packets: 1024}\n"
                                          "  propagation_long: {delay_ms: 6, limit_packets: 1024}\n")
    cmd("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes", "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
        "-keyout", str(args.out / "key.pem"), "-out", str(args.out / "cert.pem"), capture_output=True)
    dump(args.out / "environment.json", dict(testbed=TESTBED_COMMIT, kernel=os.uname().release,
         testbed_hashes={name: sha(testbed/name) for name in files}, endpoint_hash=sha(args.endpoint),
         qlog=not args.no_qlog, cases=args.only, repeats=args.repeats))
    logger = TrafficLogger(str(args.out / "traffic.sqlite"))
    with topology() as namespaces:
        for repeat in range(args.repeats):
            cases = list(args.only)
            random.Random(8100+repeat).shuffle(cases)
            for case in cases:
                plan = scenario(case, 9911+repeat, fixtures)
                run_case(args.out, case, repeat, plan, namespaces, args.endpoint.resolve(), NetworkEmulator,
                         logger, not args.no_qlog and repeat == 0)


if __name__ == "__main__":
    main()
