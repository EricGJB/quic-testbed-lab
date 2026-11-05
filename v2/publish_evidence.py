"""Create the V2 public evidence bundle from approved trace fields only."""

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path

SELECTED = {
    "steady", "steady_cap8", "rate10_high", "rate50_high", "rate50_low", "rate50_matched",
    "rate50_opposite", "delay10", "delay50", "ack_variation", "urban", "congested",
    "grant_replay", "grant_inverse", "delay10_fifo", "delay_slot30",
}
FILES = ("run.json", "client.qlog.json.gz", "server.qlog.json.gz", "tx.pcap.gz", "rx.pcap.gz",
         "tx.capture.txt", "rx.capture.txt")


def add(archive, name, data):
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = 0o644
    archive.addfile(member, io.BytesIO(data))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("raw", type=Path)
    parser.add_argument("repeat", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    manifest = {}
    with tarfile.open(args.out, "w:xz") as archive:
        for prefix, root in (("formal",args.raw),("repeat",args.repeat)):
            files = [root / name for name in ("environment.json","traffic.sqlite")]
            for run in root.glob("*/run.json"):
                files.append(run)
                case, ident = run.parent.name.split("__")
                if case in SELECTED and ident == "01_1":
                    files.extend(run.parent / name for name in FILES if name != "run.json")
            for path in sorted(files):
                if path.is_file():
                    name = prefix + "/" + path.relative_to(root).as_posix()
                    data = path.read_bytes()
                    add(archive,name,data)
                    manifest[name] = hashlib.sha256(data).hexdigest()
        add(archive,"SHA256-MANIFEST.json",json.dumps(manifest,indent=2).encode())
    print(len(manifest), "public members,", args.out.stat().st_size,"bytes")


if __name__ == "__main__":
    main()
