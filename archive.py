"""Archive only audited measurement files; never traverse arbitrary local inputs."""

import argparse
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

RUN_FILES = ("run.json", "received.json", "sent.json", "tx.pcap.gz", "rx.pcap.gz",
             "tx.capture.txt", "rx.capture.txt", "client.qlog.json.gz", "server.qlog.json.gz",
             "failure_public.json")
SUITE_FILES = ("environment.json", "traffic.sqlite", "grace_frames.csv", "media.json")


def add_bytes(archive, name, data):
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = 0o644
    archive.addfile(member, io.BytesIO(data))


def bundle(raw, destination):
    manifest = {}
    with destination.open("wb") as file, gzip.GzipFile(filename="", mode="wb", fileobj=file, mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w|") as archive:
            for sub in (Path("."), Path("followup"), Path("no-qlog"), Path("diagnostic-attempts")):
                suite = raw / sub
                paths = [suite / name for name in SUITE_FILES]
                runs = {p.parent for name in ("run.json", "failure_public.json") for p in suite.glob(f"*/{name}")}
                for run in sorted(runs):
                    paths.extend(run / name for name in RUN_FILES)
                for path in paths:
                    if path.is_file():
                        data = path.read_bytes()
                        name = path.relative_to(raw).as_posix()
                        add_bytes(archive, name, data)
                        manifest[name] = hashlib.sha256(data).hexdigest()
            add_bytes(archive, "SHA256-MANIFEST.json", json.dumps(manifest, indent=2).encode())
    return len(manifest)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("raw", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    count = bundle(args.raw, args.destination)
    print(count, "files;", args.destination.stat().st_size, "compressed bytes")


if __name__ == "__main__":
    main()
