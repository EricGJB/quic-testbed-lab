"""Drive the upstream Testbed emulator and traffic logger from an isolated router."""

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import aioquic
import yaml

from endpoint import PORT, SERVER

ROOT = Path(__file__).resolve().parent
TESTBED_COMMIT = "7231ae8e5670b67564cf28d397c2820d9403ecdc"


def cmd(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def output(*args):
    return cmd(*args, capture_output=True).stdout


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, separators=(",", ":")))
    tmp.replace(path)


@contextmanager
def topology():
    namespaces = ["ql" + str(os.getpid()) + suffix for suffix in ("c", "r", "s")]
    client, router, server = namespaces
    created = []
    try:
        for ns in namespaces:
            cmd("ip", "netns", "add", ns)
            created.append(ns)
            cmd("ip", "-n", ns, "link", "set", "lo", "up")
        for ns, ep, routed, network in ((client, "c0", "r0", "10.206.0"),
                                        (server, "s0", "r1", "10.207.0")):
            cmd("ip", "link", "add", ep, "netns", ns, "type", "veth",
                "peer", "name", routed, "netns", router)
            for namespace, iface, suffix in ((ns, ep, ".2/24"), (router, routed, ".1/24")):
                cmd("ip", "-n", namespace, "address", "add", network + suffix, "dev", iface)
                cmd("ip", "-n", namespace, "link", "set", iface, "up")
                cmd("ip", "netns", "exec", namespace, "ethtool", "-K", iface,
                    "gro", "off", "gso", "off", "tso", "off", capture_output=True)
                features = output("ip", "netns", "exec", namespace, "ethtool", "-k", iface)
                if any(f"{name}: on" in features for name in (
                    "generic-receive-offload", "generic-segmentation-offload", "tcp-segmentation-offload"
                )):
                    raise RuntimeError("offload remained enabled")
            cmd("ip", "-n", ns, "route", "add", "default", "via", network + ".1")
        cmd("ip", "netns", "exec", router, "sysctl", "-qw", "net.ipv4.ip_forward=1")
        yield namespaces
    finally:
        for ns in reversed(created):
            cmd("ip", "netns", "del", ns)


@contextmanager
def in_namespace(namespace):
    with open("/proc/self/ns/net", "rb") as original, open("/run/netns/" + namespace, "rb") as target:
        os.setns(target.fileno(), os.CLONE_NEWNET)
        try:
            yield
        finally:
            os.setns(original.fileno(), os.CLONE_NEWNET)


def net_stats(router):
    start = time.monotonic()
    result = json.loads(output("ip", "netns", "exec", router, "tc", "-s", "-j", "qdisc", "show"))
    return [start, time.monotonic(), [r for r in result if r["dev"] in ("r0", "r1")]]


def schedule(profile):
    for key in ("rate_step", "delay_step"):
        if key in profile:
            high, low = profile[key]
            for index, stamp in enumerate(range(profile["begin_ms"], profile["end_ms"], profile["dwell_ms"])):
                yield stamp, key, (low, high)[index % 2]
            yield profile["end_ms"], key, high


def change(router, kind, value, settings):
    if kind == "rate_step":
        commands = [["class", "change", "dev", "r1", "parent", parent, "classid", handle,
                     "htb", "rate", f"{value}mbit", "ceil", f"{value}mbit"]
                    for parent, handle in (("1:", "1:1"), ("1:1", "1:11"))]
        batch = "\n".join(" ".join(parts) for parts in commands) + "\n"
        cmd("ip", "netns", "exec", router, "tc", "-batch", "-", input=batch, capture_output=True)
    else:
        cmd("ip", "netns", "exec", router, "tc", "qdisc", "change", "dev", "r1",
            "parent", "1:11", "handle", "10:", "netem", "delay", f"{value}ms",
            "limit", str(settings["limit_packets"]), capture_output=True)


def wait_ready(path, process, log, timeout=15):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError(f"{path.name}: {log.read_text()[-3500:]}")
        time.sleep(.005)


def start_capture(namespace, iface, path, log):
    with log.open("w") as handle:
        return subprocess.Popen(["ip", "netns", "exec", namespace, "tcpdump", "-i", iface,
                                 "-Z", "root", "-U", "-n", "-s", "96", "--time-stamp-precision=nano",
                                 "-w", str(path), "port", str(PORT)],
                                stdout=subprocess.DEVNULL, stderr=handle)


def stop_process(process, sig):
    if process.poll() is None:
        process.send_signal(sig)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def gzip_capture(path):
    with path.open("rb") as source, gzip.open(str(path) + ".gz", "wb") as target:
        shutil.copyfileobj(source, target)
    path.unlink()


def seal_failure(work, logger):
    """Keep startup timeouts in the denominator and retain available wire evidence."""
    original = work / "failed.json"
    error = json.loads(original.read_text())["error"]
    workload, algorithm, profile, index = work.name.split("__")
    stage = "startup_timeout" if "verified.wait()" in error and "TimeoutError" in error else "run_error"
    for side in ("tx", "rx"):
        pcap = work / (side + ".pcap")
        if pcap.exists():
            gzip_capture(pcap)
    failure = dict(workload=workload, algorithm=algorithm, profile=profile, run_index=int(index),
                   stage=stage, receiver_started=(work / "server_ready").exists(),
                   workload_started=(work / "start").exists())
    from analysis.logger import LogRecord
    with sqlite3.connect(logger.db_path) as db:
        existing = db.execute("SELECT success FROM traffic_logs WHERE session_id=?", (work.name,)).fetchone()
    if existing is None:
        logger.log(LogRecord(timestamp=original.stat().st_mtime, scenario_id="quic_transport_" + workload,
                             session_id=work.name, turn_index=0, run_index=int(index), provider=algorithm,
                             model="GRACE-4096" if workload == "grace" else "fixed-bytes",
                             network_profile=profile, success=False, error_type=stage,
                             metadata=json.dumps(failure)))
    dump(work / "failure_public.json", failure)
    return stage == "startup_timeout" and workload == "grace" and profile == "congested"


def run_case(args, namespaces, catalog, case, algorithm, workload, index, logger, emulator):
    client, router, server = namespaces
    work = args.out / f"{workload}__{algorithm}__{case}__{index:02d}"
    if (work / "run.json").exists() or (work / "failed.json").exists():
        return
    work.mkdir(exist_ok=True)
    for filename in ("start", "stop", "client_ready", "server_ready", "failed.json"):
        (work / filename).unlink(missing_ok=True)
    profile = catalog["scenarios"][case]
    def apply_profiles(clean=False):
        with in_namespace(router):
            for iface, direction in (("r1", "forward"), ("r0", "reverse")):
                name = "no_emulation" if clean else profile[direction]
                shaper = emulator(interface=iface, profiles_path=str(args.out / "applied_profiles.yaml"),
                                  bidirectional=False)
                if not shaper.apply_profile(name):
                    raise RuntimeError("Testbed failed applying " + name)
    clean_bootstrap = profile.get("bootstrap_clean", False)
    apply_profiles(clean=clean_bootstrap)
    before = net_stats(router)
    duration = args.duration if workload == "bulk" else 2.3
    endpoints = []
    captures = []
    started = time.time()
    switches = []
    samples = []
    tcp_samples = []
    try:
        for side, namespace, iface in (("tx", client, "c0"), ("rx", server, "s0")):
            cap = start_capture(namespace, iface, work / (side + ".pcap"), work / (side + ".capture.txt"))
            captures.append(cap)
            deadline = time.monotonic() + 3
            while "listening on" not in (work / (side + ".capture.txt")).read_text():
                if cap.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"{side} capture failed")
                time.sleep(.005)
        for role, namespace in (("server", server), ("client", client)):
            log = work / (role + ".txt")
            with log.open("w") as handle:
                launch = ["ip", "netns", "exec", namespace, sys.executable, str(args.endpoint),
                          role, str(work), workload, algorithm, str(duration)]
                if args.no_qlog:
                    launch.append("--no-qlog")
                process = subprocess.Popen(launch, stdout=handle, stderr=subprocess.STDOUT)
            endpoints.append(process)
            wait_ready(work / (role + "_ready"), process, log)
        if clean_bootstrap:
            apply_profiles()
            before = net_stats(router)
        start = time.monotonic() + .18
        wall_minus_mono = time.time() - time.monotonic()
        (work / "start").write_text(str(start))
        changes = list(schedule(profile))
        cursor = 0
        next_sample = start
        stop = start + duration
        while time.monotonic() < stop:
            now = time.monotonic()
            target = start + changes[cursor][0] / 1000 if cursor < len(changes) else stop
            if target < stop and target <= now:
                planned, kind, value = changes[cursor]
                began = time.monotonic()
                change(router, kind, value, catalog["profiles"][profile["forward"]])
                switches.append([planned, (began - start) * 1000, (time.monotonic() - start) * 1000, kind, value])
                cursor += 1
            elif next_sample <= now and (target - now >= .005 or target == stop):
                samples.append(net_stats(router))
                if algorithm == "tcp":
                    tcp_samples.append([time.monotonic(), output("ip", "netns", "exec", client, "ss", "-tinH",
                                                                 "dst", SERVER)])
                next_sample += .1
            else:
                wake = min(target, stop, next_sample if next_sample > now else target)
                time.sleep(max(0, wake - time.monotonic()))
        if workload == "bulk":
            time.sleep(.45)
        else:
            time.sleep(.35)
        after = net_stats(router)
        (work / "stop").touch()
        for role, process in zip(("server", "client"), endpoints):
            if process.wait(timeout=7) != 0:
                raise RuntimeError((work / (role + ".txt")).read_text()[-3500:])
        for cap in captures:
            stop_process(cap, signal.SIGINT)
        for side in ("tx", "rx"):
            log = (work / (side + ".capture.txt")).read_text()
            if not re.search(r"^0 packets dropped by kernel", log, re.M):
                raise RuntimeError("capture did not exit cleanly: " + log)
            pcap = work / (side + ".pcap")
            gzip_capture(pcap)
        received = json.loads((work / "received.json").read_text())
        if workload == "bulk" and (received["corrupt_bytes"] or not received["total"]):
            raise ValueError("bulk received data integrity")
        if workload == "grace" and (not received["iframe_verified"] or
                                     len(received["frames"]) != len(json.loads((args.out / "media.json").read_text())["frames"])):
            raise ValueError("GRACE receiver manifest")
        run = dict(profile=case, algorithm=algorithm, workload=workload, run_index=index, start=start,
                   wall_minus_mono=wall_minus_mono, duration=duration, switches=switches,
                   qdisc_before=before, qdisc_after=after, qdisc_samples=samples, tcp_samples=tcp_samples)
        from analysis.logger import LogRecord
        request_bytes = received["total"] if workload == "bulk" else 0
        logger.log(LogRecord(timestamp=started, scenario_id="quic_transport_" + workload,
                             session_id=work.name, turn_index=0, run_index=index, provider=algorithm,
                             model="GRACE-4096" if workload == "grace" else "fixed-bytes",
                             request_bytes=request_bytes, network_profile=case, success=True,
                             metadata=json.dumps({"evidence_dir": work.name, "receiver_bytes": request_bytes})))
        dump(work / "run.json", run)
        print(work.name, "ok", flush=True)
    finally:
        for process in endpoints:
            stop_process(process, signal.SIGTERM)
        for process in captures:
            stop_process(process, signal.SIGINT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--testbed", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workload", choices=["bulk", "grace"], required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--duration", type=float, default=5.8)
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--algorithm", nargs="+")
    parser.add_argument("--no-qlog", action="store_true")
    parser.add_argument("--suite", type=Path, default=ROOT / "profiles.yaml")
    parser.add_argument("--endpoint", type=Path, default=ROOT / "endpoint.py")
    parser.add_argument("--finalize-failures-only", action="store_true")
    args = parser.parse_args()
    if os.geteuid() or aioquic.__version__ != "1.2.0":
        parser.error("requires root for isolated netns and aioquic==1.2.0")
    args.out = args.out.resolve()
    args.testbed = args.testbed.resolve()
    args.endpoint = args.endpoint.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    sys.path[:0] = [str(args.testbed / "netemu/src"), str(args.testbed / "aitestbed")]
    from netemu import NetworkEmulator
    from analysis.logger import TrafficLogger
    if args.finalize_failures_only:
        logger = TrafficLogger(str(args.out / "traffic.sqlite"))
        count = sum(seal_failure(path.parent, logger) for path in sorted(args.out.glob("*/failed.json")))
        print(count, "startup timeout records retained")
        return
    paths = ["netemu/src/netemu/emulator.py", "netemu/src/netemu/profile.py",
             "aitestbed/analysis/logger.py", "aitestbed/configs/profiles.yaml"]
    for path in paths:
        blob = subprocess.check_output(["git", "-c", "safe.directory=" + str(args.testbed),
                                        "-C", str(args.testbed), "show", f"{TESTBED_COMMIT}:{path}"])
        if blob != (args.testbed / path).read_bytes().replace(b"\r\n", b"\n"):
            raise ValueError("Testbed checkout differs from pinned upstream: " + path)
    catalog = yaml.safe_load(args.suite.read_text())
    catalog["profiles"] = yaml.safe_load((args.testbed / paths[-1]).read_text())["profiles"] | catalog["profiles"]
    (args.out / "applied_profiles.yaml").write_text(yaml.safe_dump({"profiles": catalog["profiles"]}))
    if not (args.out / "cert.pem").exists():
        cmd("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
            "-keyout", str(args.out / "key.pem"), "-out", str(args.out / "cert.pem"), capture_output=True)
    dump(args.out / "environment.json", dict(kernel=platform.release(), python=platform.python_version(),
         aioquic=aioquic.__version__, testbed_commit=TESTBED_COMMIT,
         sources={path: sha(args.testbed / path) for path in paths},
         lab_profiles=sha(args.suite), qlog=not args.no_qlog,
         endpoint_sha256=sha(args.endpoint),
         tc=output("tc", "-V").strip(), repeats=args.repeats))
    logger = TrafficLogger(str(args.out / "traffic.sqlite"))
    matrix = catalog["matrix"][args.workload]
    failed = 0
    with topology() as namespaces:
        for index in range(args.repeats):
            cases = [(algorithm, case) for algorithm, profile_names in matrix.items()
                     for case in profile_names if (not args.only or case in args.only)
                     and (not args.algorithm or algorithm in args.algorithm)]
            # Fixed shuffle controls execution-order effects without changing the cell denominator.
            import random
            random.Random(20261105 + index).shuffle(cases)
            for algorithm, case in cases:
                try:
                    run_case(args, namespaces, catalog, case, algorithm, args.workload, index, logger, NetworkEmulator)
                except Exception as error:
                    work = args.out / f"{args.workload}__{algorithm}__{case}__{index:02d}"
                    dump(work / "failed.json", {"error": str(error)})
                    print(work.name, "FAILED", error, flush=True)
                    failed += not seal_failure(work, logger)
    raise SystemExit(int(failed > 0))


if __name__ == "__main__":
    main()
