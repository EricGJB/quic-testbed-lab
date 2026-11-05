"""Archive only audited measurement files; never traverse arbitrary local inputs."""

import argparse
import gzip
import hashlib
import io
import json
import lzma
import tarfile
from pathlib import Path

RUN_FILES = ("run.json", "received.json", "sent.json", "tx.pcap.gz", "rx.pcap.gz",
             "tx.capture.txt", "rx.capture.txt", "client.qlog.json.gz", "server.qlog.json.gz",
             "failure_public.json")
SUITE_FILES = ("environment.json", "traffic.sqlite", "grace_frames.csv", "media.json")
PUBLIC_WIRE_RUNS = {
    "bulk__cubic__delay10ms__00", "bulk__cubic__delayjitter__00",
    "bulk__cubic_pkt12__delay10ms__00", "grace__cubic__model_delay10ms__00",
    "grace__cubic__model_rate10ms__00", "grace__cubic__model_rate100ms__00",
    "grace__cubic__model_congested_preroll__00", "grace__cubic__congested__00",
}
WIRE_FILES = {"tx.pcap.gz", "rx.pcap.gz", "client.qlog.json.gz", "server.qlog.json.gz"}


def add_bytes(archive, name, data):
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = 0o644
    archive.addfile(member, io.BytesIO(data))


def bundle(raw, destination, compact=False):
    manifest = {}
    with destination.open("wb") as file:
        zipped = lzma.LZMAFile(file, "w", preset=6) if compact else gzip.GzipFile(
            filename="", mode="wb", fileobj=file, mtime=0)
        with zipped:
            archive_runs(raw, zipped, manifest, compact)
    return len(manifest)


def archive_runs(raw, zipped, manifest, compact):
    with tarfile.open(fileobj=zipped, mode="w|") as archive:
        for sub in (Path("."), Path("followup"), Path("no-qlog"), Path("diagnostic-attempts")):
            suite = raw / sub
            paths = [suite / name for name in SUITE_FILES]
            runs = {p.parent for name in ("run.json", "failure_public.json") for p in suite.glob(f"*/{name}")}
            for run in sorted(runs):
                paths.extend(run / name for name in RUN_FILES if not compact
                             or name not in WIRE_FILES or run.name in PUBLIC_WIRE_RUNS)
            for path in paths:
                if path.is_file():
                    data = path.read_bytes()
                    if compact and path.name == "sent.json" and path.parent.name not in PUBLIC_WIRE_RUNS:
                        send = json.loads(data)
                        for name in ("pacing_waits", "pacing_wakes"):
                            if name in send:
                                send[name + "_count"] = len(send.pop(name))
                        data = json.dumps(send, separators=(",", ":")).encode()
                    name = path.relative_to(raw).as_posix()
                    add_bytes(archive, name, data)
                    manifest[name] = hashlib.sha256(data).hexdigest()
        add_bytes(archive, "SHA256-MANIFEST.json", json.dumps(manifest, indent=2).encode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("raw", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    count = bundle(args.raw, args.destination, args.compact)
    print(count, "files;", args.destination.stat().st_size, "compressed bytes")


if __name__ == "__main__":
    main()
