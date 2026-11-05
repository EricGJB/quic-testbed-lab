"""Run paired multimodal QUIC experiments through the upstream Testbed emulator."""

import argparse
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import aioquic
import yaml

from workload import MODEL_PORT, ROOT, dump, request_body, sha
from analyze import packets
from run import (TESTBED_COMMIT, change, cmd, gzip_capture, in_namespace,
                 net_stats, start_capture, stop_process, topology, wait_ready)


def wait_model(namespace, process, log):
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline and process.poll() is None:
        request = subprocess.run(["ip", "netns", "exec", namespace, "curl", "-fsS",
                                  "--max-time", "1", f"http://127.0.0.1:{MODEL_PORT}/health"],
                                 capture_output=True)
        if request.returncode == 0:
            return
        time.sleep(.3)
    raise RuntimeError(log.read_text(errors="replace")[-3500:])


def grant_pattern(reference, row, inverse):
    """Allocate one high and one low 50 ms grant per 100 ms from a past trace."""
    work = reference / f"steady__{row['video_id']:02d}_{row['question_number']}"
    run = json.loads((work / "run.json").read_text())
    client = json.loads((work / "client.json").read_text())
    buckets = [0] * 80
    for stamp, direction, size, _ in packets(work / "tx.pcap.gz", client["start"] + run["wall_minus_mono"]):
        if direction == "forward" and 0 <= stamp < 4:
            buckets[int(stamp/.05)] += size
    rates = []
    for first, second in zip(buckets[::2], buckets[1::2]):
        high_first = (first >= second) != inverse
        rates.extend((14, 2) if high_first else (2, 14))
    return rates, buckets


def spin_case(out, case, profile, row, body, namespace, emulator, catalog, logger):
    from analysis.logger import LogRecord
    profile = dict(profile, forward_mbps=catalog[profile["forward"]].get("rate_mbit"))
    work = out / f"{case}__{row['video_id']:02d}_{row['question_number']}"
    if (work / "run.json").exists():
        return
    work.mkdir(exist_ok=False)
    (work / "input.bin").write_bytes(body)
    dump(work / "profile.json", profile)
    client_ns, router, server_ns = namespace
    kind = "rate" if "rate" in profile else "delay" if "delay" in profile else None
    pattern = profile.get("service_pattern", profile.get(kind))
    with in_namespace(router):
        for iface, side in (("r1", "forward"), ("r0", "reverse")):
            shaper = emulator(interface=iface, profiles_path=str(out / "applied_profiles.yaml"), bidirectional=False)
            name = "service8" if kind and side == "forward" else profile[side]
            if not shaper.apply_profile(name):
                raise RuntimeError("Testbed network profile application failed")
        if profile.get("fifo"):
            cmd("tc","qdisc","add","dev","r1","parent","10:1","handle","11:","pfifo",
                "limit",str(catalog[profile["forward"]]["limit_packets"]),capture_output=True)
    before = net_stats(router)
    captures = []
    processes = []
    switches = []
    began = time.time()
    offset = time.time() - time.monotonic()
    target = None
    error = None
    try:
        for side, ns, iface in (("tx", client_ns, "c0"), ("rx", server_ns, "s0")):
            capture_log = work / (side + ".capture.txt")
            cap = start_capture(ns, iface, work / (side + ".pcap"), capture_log)
            captures.append(cap)
            deadline = time.monotonic() + 2
            while "listening on" not in capture_log.read_text():
                if cap.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("capture did not start")
                time.sleep(.003)
        for role, ns in (("server", server_ns), ("client", client_ns)):
            log = work / (role + ".txt")
            with log.open("w") as stdout:
                proc = subprocess.Popen(["ip", "netns", "exec", ns, sys.executable,
                                         str(Path(__file__).with_name("workload.py")), role, str(work)],
                                        stdout=stdout, stderr=subprocess.STDOUT)
            processes.append(proc)
            wait_ready(work / (role + "_ready"), proc, log, timeout=10)
        if kind:
            change(router, kind + "_step", pattern[0], catalog[profile["forward"]])
        if profile.get("slot"):
            cmd("ip","netns","exec",router,"tc","qdisc","change","dev","r1",
                "parent","1:11","handle","10:","netem","delay","5ms","limit","128",
                "slot",f"{profile['slot']}ms",f"{profile['slot']}ms",capture_output=True)
        target = time.monotonic() + .035
        (work / "start").write_text(str(target))
        if kind:
            for index in range(1, 4000 // profile["dwell"]):
                when = target + index * profile["dwell"] / 1000
                if (work / "upload_done").exists():
                    break
                time.sleep(max(0, when-time.monotonic()))
                if (work / "upload_done").exists():
                    break
                start = time.monotonic()
                value = pattern[index % len(pattern)]
                change(router, kind + "_step", value, catalog[profile["forward"]])
                switches.append((index*profile["dwell"], (start-target)*1000,
                                 (time.monotonic()-target)*1000, kind, value))
        if processes[-1].wait(timeout=11):
            raise RuntimeError("QUIC client did not exit successfully")
    except Exception as problem:
        error = type(problem).__name__
        (work / "failure-local.txt").write_text(str(problem))
    finally:
        (work / "stop").touch()
        for process in processes:
            if process is processes[-1]:
                stop_process(process, signal.SIGTERM)
            elif wait_server(process):
                error = error or "server_exit"
        for capture in captures:
            stop_process(capture, signal.SIGINT)
        (work / "input.bin").unlink(missing_ok=True)
    after = net_stats(router)
    for side in ("tx", "rx"):
        path = work / (side + ".pcap")
        if path.exists():
            log = (work / (side + ".capture.txt")).read_text()
            if not re.search(r"^0 packets dropped by kernel", log, re.M):
                raise RuntimeError("pcap evidence lost")
            gzip_capture(path)
    client = json.loads((work / "client.json").read_text()) if (work / "client.json").exists() else {}
    server = json.loads((work / "server.json").read_text()) if (work / "server.json").exists() else {}
    if server and server["sha256"] != sha(body):
        raise ValueError("uploaded body SHA-256 mismatch")
    status = client.get("status", "handshake_or_runner_failure") if not error else "handshake_or_runner_failure"
    result = dict(case=case, algorithm=profile.get("algorithm", "cubic"), question_id=row["question_id"],
                  video=row["video_id"], label=row["answer"], status=status, input_bytes=len(body),
                  sha256=sha(body), target=target, wall_minus_mono=offset, switches=switches,
                  qdisc_before=before, qdisc_after=after, verified_upload=bool(server), profile=profile,
                  error=error)
    dump(work / "run.json", result)
    logger.log(LogRecord(timestamp=began, scenario_id="quic_multimodal_qa", session_id=work.name, turn_index=0,
                         network_profile=case, provider="aioquic-local", model="MiniCPM-o-2.6-Q4_K_M",
                         request_bytes=len(body), response_bytes=client.get("answer_chars", 0),
                         success=status == "complete", metadata=json.dumps({"evidence": work.name, "status": status})))
    print(work.name, status, client.get("prediction"), flush=True)


def wait_server(process):
    try:
        return process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        stop_process(process, signal.SIGTERM)
        return process.returncode


def main():
    parser = argparse.ArgumentParser()
    for name in ("testbed", "out", "assets", "llama"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--lib-path", required=True)
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--grant-reference", type=Path)
    args = parser.parse_args()
    if os.geteuid() or aioquic.__version__ != "1.2.0":
        parser.error("root and aioquic 1.2.0 are required")
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    sys.path[:0] = [str(args.testbed / "netemu/src"), str(args.testbed / "aitestbed")]
    from netemu import NetworkEmulator
    from analysis.logger import TrafficLogger
    for file in ("netemu/src/netemu/emulator.py", "netemu/src/netemu/profile.py",
                 "aitestbed/analysis/logger.py", "aitestbed/configs/profiles.yaml"):
        source = subprocess.check_output(["git", "-c", "safe.directory=" + str(args.testbed),
                                          "-C", str(args.testbed), "show", f"{TESTBED_COMMIT}:{file}"])
        if (args.testbed / file).read_bytes().replace(b"\r\n", b"\n") != source:
            raise ValueError("Testbed source differs from pinned version")
    definitions = yaml.safe_load(Path(__file__).with_name("profiles.yaml").read_text())
    if args.grant_reference:
        for name in ("grant_replay", "grant_inverse"):
            definitions["scenarios"][name] = dict(forward="service8", reverse="ack20", rate=[14,2], dwell=50)
        definitions["scenarios"]["delay10_fifo"] = dict(definitions["scenarios"]["delay10"],fifo=True)
        definitions["scenarios"]["delay_slot30"] = dict(forward="service8",reverse="ack20",slot=30)
    catalog = yaml.safe_load((args.testbed / "aitestbed/configs/profiles.yaml").read_text())["profiles"]
    catalog.update(definitions["profiles"])
    (args.out / "applied_profiles.yaml").write_text(yaml.safe_dump({"profiles": catalog}))
    cert, key = args.out / "cert.pem", args.out / "key.pem"
    if not cert.exists():
        cmd("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
            "-keyout", str(key), "-out", str(cert), capture_output=True)
    path = args.assets / "results/q4_k_m_four_native/questions_manifest.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    measured = [row for row in rows if row["video_id"] in (1, 4, 7, 10, 13, 16)
                and row["question_number"] in (1, 2)]
    warmup = [row for row in rows if row["video_id"] == 2 and row["question_number"] in (1, 2)]
    if len(measured) != 12 or len(warmup) != 2:
        raise ValueError("QA question selection mismatch")
    if args.pilot:
        measured = measured[:1]
    prompts = [(row, request_body(args.assets, row)) for row in measured]
    model = args.assets / "models/Model-7.6B-Q4_K_M.gguf"
    projector = args.assets / "models/mmproj-model-f16.gguf"
    selected = [case for case in definitions["scenarios"] if not args.only or case in args.only]
    previous_env = args.out / "environment.json"
    if previous_env.exists():
        selected = sorted(set(selected) | set(json.loads(previous_env.read_text())["cases"]))
    environment = dict(testbed_commit=TESTBED_COMMIT, aioquic=aioquic.__version__, model="MiniCPM-o 2.6",
                       llama_build="11516", q4="Q4_K_M", projector="F16", mode="four past native frames",
                       kernel=os.uname().release, cases=selected, questions=len(prompts))
    dump(args.out / "environment.json", environment)
    logger = TrafficLogger(str(args.out / "traffic.sqlite"))
    with topology() as ns:
        log = args.out / "model-local.txt"
        with log.open("w") as stdout:
            launch = ["ip", "netns", "exec", ns[2], str(args.llama), "-m", str(model),
                      "--mmproj", str(projector), "-ngl", "99", "-c", "4096", "-np", "1",
                      "-b", "512", "-ub", "256", "-t", "4", "-tb", "4", "--host", "127.0.0.1",
                      "--port", str(MODEL_PORT), "--no-webui"]
            process = subprocess.Popen(launch, stdout=stdout, stderr=subprocess.STDOUT,
                                       env=dict(os.environ, LD_LIBRARY_PATH=args.lib_path))
        try:
            wait_model(ns[2], process, log)
            for row in warmup:
                fixture = args.out / "warmup-local.bin"
                fixture.write_bytes(request_body(args.assets, row))
                probe = ("import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                         "from workload import infer;res=infer(Path(sys.argv[2]).read_bytes(),lambda _:None);"
                         "assert res['tokens'];print('model warmup visible text',len(res['tokens']))")
                cmd("ip", "netns", "exec", ns[2], sys.executable, "-c", probe,
                    str(Path(__file__).parent), str(fixture))
                fixture.unlink()
            print("READY: warmed MiniCPM in isolated receiver namespace", flush=True)
            for index, (row, body) in enumerate(prompts):
                cases = [(case, profile) for case, profile in definitions["scenarios"].items()
                         if not args.only or case in args.only]
                random.Random(20261105 + index + (1000 if args.grant_reference else 0)).shuffle(cases)
                for case, profile in cases:
                    if case in ("grant_replay", "grant_inverse"):
                        pattern, offered = grant_pattern(args.grant_reference, row, case == "grant_inverse")
                        profile = dict(profile, service_pattern=pattern, prior_50ms_offered_ip_bytes=offered)
                    spin_case(args.out, case, profile, row, body, ns, NetworkEmulator, catalog, logger)
        finally:
            stop_process(process, signal.SIGTERM)


if __name__ == "__main__":
    main()
