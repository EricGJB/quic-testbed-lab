"""Prepare two real four-JPEG sizes, and optionally score them on the local VLM."""

import argparse
import hashlib
import io
import itertools
import json
import os
import signal
import struct
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "v2"), str(ROOT)]
from run import dump, stop_process, topology
from run_qa import wait_model
from workload import MODEL_PORT, infer, parse_body, request_body, sha


def encode(image, scale, quality):
    source = Image.open(io.BytesIO(image)).convert("RGB")
    size = tuple(max(28, int(dim*scale/28)*28) for dim in source.size)
    source = source.resize(size, Image.Resampling.LANCZOS)
    output = io.BytesIO()
    source.save(output, "JPEG", quality=quality, optimize=True)
    return output.getvalue()


def pack(header, images):
    header = json.dumps(dict(header, lengths=list(map(len, images)), hashes=list(map(sha, images)))).encode()
    return struct.pack("!I", len(header)) + header + b"".join(images)


def fnv(data):
    value = 0
    for byte in data:
        value = ((value ^ byte)*1099511628211) & ((1 << 64)-1)
    return value


def prepare(assets, out):
    rows = json.loads((assets / "results/q4_k_m_four_native/questions_manifest.json").read_text())
    catalog = {}
    for row in rows:
        if row["video_id"] not in (1, 4, 7, 10, 13, 16) or row["question_number"] not in (1, 2):
            continue
        key = f"{row['video_id']:02}_{row['question_number']}"
        header, originals = parse_body(request_body(assets, row))
        grid = list(itertools.product((.45,.55,.65,.75,.85,1), (32,40,48,56,64,72,80,88)))
        candidates = [(pack(header, [encode(im, scale, quality) for im in originals]), scale, quality)
                      for scale, quality in grid]
        catalog[key] = {"label": row["answer"]}
        for tier, target in (("low", 18_000), ("high", 40_000)):
            body, scale, quality = min(candidates, key=lambda c: abs(len(c[0])-target))
            path = out / f"{key}-{tier}.bin"
            path.write_bytes(body)
            catalog[key][tier] = dict(bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
                wire_hash=fnv(body), path=str(path), scale=scale, jpeg_quality=quality)
    assert len(catalog) == 12
    dump(out / "fixtures.json", catalog)
    return catalog


def score(args, catalog):
    model = args.assets / "models/Model-7.6B-Q4_K_M.gguf"
    projector = args.assets / "models/mmproj-model-f16.gguf"
    with topology() as (_, _, receiver), (args.out / "model.log").open("w") as log:
        process = subprocess.Popen(["ip", "netns", "exec", receiver, str(args.llama),
            "-m", str(model), "--mmproj", str(projector), "-ngl", "99", "-c", "4096", "-np", "1",
            "-b", "512", "-ub", "256", "-t", "4", "-tb", "4", "--host", "127.0.0.1",
            "--port", str(MODEL_PORT), "--no-webui"], stdout=log, stderr=subprocess.STDOUT,
            env=dict(os.environ, LD_LIBRARY_PATH=args.lib_path))
        try:
            wait_model(receiver, process, args.out / "model.log")
            def infer_fixture(fixture):
                code = ("import sys,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                        "from workload import infer;print(json.dumps(infer(Path(sys.argv[2]).read_bytes(),lambda _:None)))")
                result = subprocess.check_output(["ip", "netns", "exec", receiver, sys.executable, "-c", code,
                    str(ROOT/"v2"), fixture["path"]], text=True)
                return json.loads(result)
            first = next(iter(catalog.values()))
            for tier in ("low", "high"):
                infer_fixture(first[tier])
            results = []
            for key, tiers in catalog.items():
                for tier in ("low", "high"):
                    result = infer_fixture(tiers[tier])
                    results.append(dict(question=key, tier=tier, label=tiers["label"], **result))
            dump(args.out / "model.json", results)
        finally:
            stop_process(process, signal.SIGTERM)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--assets", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--llama", type=Path)
    p.add_argument("--lib-path", default="")
    args = p.parse_args()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "fixtures.json"
    catalog = json.loads(path.read_text()) if path.exists() else prepare(args.assets, args.out)
    if args.llama:
        score(args, catalog)


if __name__ == "__main__":
    main()
