"""Encode local GRACE features and score only verified blocks delivered by deadline."""

import argparse
import csv
import hashlib
import io
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

PAYLOAD = 1120
DEADLINES = (50, 100, 200)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def jpeg(data):
    return np.asarray(Image.open(io.BytesIO(data)), dtype=np.float32) / 255


def load(codec_repo, grace, cache):
    sys.path.insert(0, str(codec_repo.resolve()))
    from grace_rtp.codec import GraceCodec
    manifest = json.loads((cache / "manifest.json").read_text())
    checkpoint = grace / "models/grace/4096_freeze.model"
    codec = GraceCodec(grace, checkpoint, manifest["q"], manifest["scale"])
    if codec.checkpoint_hash != manifest["checkpoint_sha256"]:
        raise ValueError("checkpoint differs from source cache")
    return manifest, codec


def prepare(args):
    from importlib.metadata import version
    source, codec = load(args.codec_repo, args.grace, args.cache)
    from grace_rtp.blocks import compress_features
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    metadata = dict(checkpoint_sha256=codec.checkpoint_hash, torch=version("torch"),
                    grace_commit=source["grace_commit"], source_cache_manifest_sha256=digest(
                        (args.cache / "manifest.json").read_bytes()),
                    fps=source["fps"], width=source["width"], height=source["height"],
                    q=source["q"], scale=source["scale"], frames=[], iframes=[])
    stream_index = 0
    raw = bytearray()
    iframes = bytearray()
    references = {}
    for video in (1, 2, 3):
        records = [row for row in source["records"] if row["video_id"] == video and row["gop_id"] < 2]
        if len(records) != 2 * source["gop"]:
            raise ValueError("source cache does not contain two complete GOPs per clip")
        for record in records:
            fid = record["frame_id"]
            path = args.cache / f"v{video}_f{fid}.npz"
            with np.load(path) as data:
                image = data["source"]
                if record["frame_pos"] == 0:
                    iframe = data["iframe"].tobytes()
                    ref = jpeg(iframe)
                    metadata["iframes"].append(dict(video=video, gop=record["gop_id"], offset=len(iframes),
                                                    length=len(iframe), sha256=digest(iframe)))
                    iframes.extend(iframe)
                else:
                    code, shape = codec.encode(image, ref)
                    ref = codec.decode(code, ref, shape)
                    blocks = []
                    for block in compress_features(code):
                        blocks.append(dict(offset=len(raw), length=len(block),
                                           count=math.ceil(len(block) / PAYLOAD), sha256=digest(block)))
                        raw.extend(block)
                    metadata["frames"].append(dict(id=stream_index, video=video, source_frame=fid,
                        gop=record["gop_id"], send_ms=stream_index * 1000 / source["fps"], shape=shape,
                        code_length=len(code), source_sha256=digest(image.tobytes()),
                        clean_sha256=digest(ref.tobytes()), blocks=blocks))
                    references[str(stream_index)] = ref
            stream_index += 1
    metadata["iframe_sha256"] = digest(iframes)
    metadata["media_sha256"] = digest(raw)
    (output / "media.json").write_text(json.dumps(metadata, indent=2))
    (output / "media.bin").write_bytes(raw)
    (output / "iframes.bin").write_bytes(iframes)
    np.savez_compressed(output / "clean.npz", **references)
    print("GRACE", len(metadata["frames"]), "P frames,", len(raw), "compressed feature bytes", flush=True)


def score(args):
    _, codec = load(args.codec_repo, args.grace, args.cache)
    from grace_rtp.blocks import restore_features
    from grace_rtp.codec import psnr
    output = args.output
    info = json.loads((output / "media.json").read_text())
    raw, iframes = (output / "media.bin").read_bytes(), (output / "iframes.bin").read_bytes()
    if (digest(raw), digest(iframes)) != (info["media_sha256"], info["iframe_sha256"]):
        raise ValueError("media fixture hash mismatch")
    clean = np.load(output / "clean.npz")
    pictures = {}
    reference = {}
    for row in info["iframes"]:
        iframe = iframes[row["offset"]:row["offset"] + row["length"]]
        reference[row["video"], row["gop"]] = jpeg(iframe)
    for frame in info["frames"]:
        with np.load(args.cache / f"v{frame['video']}_f{frame['source_frame']}.npz") as data:
            image = data["source"]
        if digest(image.tobytes()) != frame["source_sha256"]:
            raise ValueError("source frame hash mismatch")
        pictures[frame["id"]] = image
    memo = {}
    results = []
    for path in sorted(output.glob("grace__*/run.json")):
        run = json.loads(path.read_text())
        rx = {row["id"]: row for row in json.loads((path.parent / "received.json").read_text())["frames"]}
        for deadline in DEADLINES:
            last_gop = None
            for frame in info["frames"]:
                fid = frame["id"]
                group = frame["video"], frame["gop"]
                if group != last_gop:
                    ref, history = reference[group], ""
                    last_gop = group
                blocks = []
                cutoff = frame["send_ms"] + deadline
                fragments = rx[fid]["blocks"]
                for spec, observed in zip(frame["blocks"], fragments):
                    complete = observed["complete_ms"]
                    blocks.append(raw[spec["offset"]:spec["offset"] + spec["length"]]
                                  if complete is not None and complete <= cutoff else None)
                mask = "".join("1" if block is None else "0" for block in blocks)
                history += mask
                key = (fid, history)
                if key not in memo:
                    if "1" in history:
                        code = restore_features(blocks, frame["code_length"])
                        ref = codec.decode(code, ref, frame["shape"])
                    else:
                        ref = clean[str(fid)]
                    memo[key] = (ref, psnr(pictures[fid], ref))
                ref, quality = memo[key]
                results.append(dict(run=path.parent.name, profile=run["profile"], algorithm=run["algorithm"],
                    repeat=run["run_index"], video=frame["video"], frame=fid, deadline_ms=deadline,
                    mask=mask, blocks_on_time=10-mask.count("1"),
                    fragments_on_time=sum(stamp <= cutoff for block in fragments for _, stamp, _ in block["fragments"]),
                    fragments_sent=sum(spec["count"] for spec in frame["blocks"]),
                    psnr_db=quality, clean_psnr_db=psnr(pictures[fid], clean[str(fid)])))
    if not results:
        raise ValueError("no GRACE runs to score")
    with (output / "grace_frames.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=results[0], lineterminator="\n")
        writer.writeheader()
        writer.writerows(results)
    print("GRACE score", len(results), "frame/deadline rows;", len(memo), "distinct reconstructions")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "score"])
    parser.add_argument("--codec-repo", type=Path, required=True)
    parser.add_argument("--grace", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    (prepare if args.action == "prepare" else score)(args)


if __name__ == "__main__":
    main()
