"""Prepare the past-only four-frame StreamingBench inputs from obtained source videos."""

import argparse
import ast
import csv
import hashlib
import io
import json
import math
import re
from collections import defaultdict, deque
from pathlib import Path

import av
from PIL import Image

VIDEOS = (1, 2, 4, 7, 10, 13, 16)


def questions(path):
    with path.open(encoding="utf-8-sig",newline="") as handle:
        for row in csv.DictReader(handle):
            match = re.fullmatch(r"Real-Time Visual Understanding_sample_(\d+)_(\d+)",row["question_id"])
            if not match:
                continue
            video, number = map(int,match.groups())
            if video in VIDEOS and number in (1,2):
                seconds = 0
                for part in row["time_stamp"].split(":"):
                    seconds = 60*seconds+int(part)
                options = ast.literal_eval(row["options"])
                if len(options) != 4 or row["answer"] not in ("A","B","C","D"):
                    raise ValueError("invalid multiple-choice data")
                yield dict(row,video_id=video,question_number=number,timestamp_seconds=seconds,options=options)


def save(folder, frames, row):
    if len(frames) != 4:
        raise ValueError("four past frames unavailable")
    folder.mkdir(parents=True,exist_ok=True)
    metadata = []
    for index,(frame_number,stamp,frame) in enumerate(frames):
        if stamp > row["timestamp_seconds"]+1e-8:
            raise ValueError("future-frame leakage")
        image=frame.to_image().convert("RGB")
        scale=min(1,448/math.sqrt(image.width*image.height))
        size=tuple(max(28,int(value*scale/28)*28) for value in image.size)
        image=image.resize(size,Image.Resampling.BICUBIC)
        buffer=io.BytesIO()
        image.save(buffer,format="JPEG",quality=95)
        payload=buffer.getvalue()
        name=f"{index:03d}.jpg"
        (folder/name).write_bytes(payload)
        metadata.append(dict(file=name,frame_index=frame_number,actual_seconds=stamp,
                             size=size,sha256=hashlib.sha256(payload).hexdigest()))
    (folder/"frames.json").write_text(json.dumps(metadata,indent=2),encoding="utf-8")


def extract(video, rows, output):
    pending = deque(sorted(rows,key=lambda row:row["timestamp_seconds"]))
    recent = deque(maxlen=4)
    with av.open(str(video)) as container:
        stream=container.streams.video[0]
        stream.thread_type="AUTO"
        stream.codec_context.thread_count=4
        for number,frame in enumerate(container.decode(stream)):
            stamp=float(frame.pts*stream.time_base)
            while pending and pending[0]["timestamp_seconds"] < stamp-1e-8:
                row=pending.popleft()
                save(output/row["question_id"].replace(" ","_"),recent,row)
            if not pending:
                break
            recent.append((number,stamp,frame))
        for row in pending:
            if not recent or row["timestamp_seconds"]-recent[-1][1] > 1/float(stream.average_rate)+1e-5:
                raise ValueError("query timestamp outside decoded video")
            save(output/row["question_id"].replace(" ","_"),recent,row)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--questions",type=Path,required=True)
    p.add_argument("--videos",type=Path,required=True)
    p.add_argument("--assets",type=Path,required=True)
    args=p.parse_args()
    rows=sorted(questions(args.questions),key=lambda r:(r["video_id"],r["question_number"]))
    if len(rows)!=14 or len({r["question_id"] for r in rows})!=14:
        raise ValueError("expected 14 distinct warmup and formal questions")
    groups=defaultdict(list)
    for row in rows:
        groups[row["video_id"]].append(row)
    for video,selected in groups.items():
        extract(args.videos/f"{video:03d}.mp4",selected,args.assets/"data/native_frames")
    manifest=args.assets/"results/q4_k_m_four_native/questions_manifest.json"
    manifest.parent.mkdir(parents=True,exist_ok=True)
    manifest.write_text(json.dumps(rows,ensure_ascii=True,indent=2),encoding="utf-8")


if __name__=="__main__":
    main()
