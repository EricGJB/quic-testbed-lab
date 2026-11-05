"""Four-frame question answering over an authenticated, bidirectional QUIC stream."""

import argparse
import asyncio
import base64
import gzip
import hashlib
import http.client
import json
import re
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aioquic.asyncio import connect, serve
from aioquic.quic import recovery
from aioquic.quic.logger import QuicLogger
from endpoint import PORT, SERVER, ProbePacer, Protocol, quic_config, wait_path, wait_until

MODEL_PORT = 18097
PROMPT = "Answer the multiple-choice question about the four video frames. Return only A, B, C, or D."


def sha(data):
    return hashlib.sha256(data).hexdigest()


def request_body(assets, row):
    folder = assets / "data/native_frames" / row["question_id"].replace(" ", "_")
    info = json.loads((folder / "frames.json").read_text(encoding="utf-8"))
    if isinstance(info, dict):
        info = info["frames"]
    frames = [(folder / frame["file"]).read_bytes() for frame in info[-4:]]
    if len(frames) != 4:
        raise ValueError("expected four native frames")
    header = json.dumps(dict(question=row["question"], options=row["options"],
                             lengths=list(map(len, frames)), hashes=list(map(sha, frames)))).encode()
    return struct.pack("!I", len(header)) + header + b"".join(frames)


def parse_body(body):
    size = struct.unpack_from("!I", body)[0]
    header = json.loads(body[4:4+size])
    pos = 4 + size
    images = []
    for length, expected in zip(header["lengths"], header["hashes"], strict=True):
        image = body[pos:pos+length]
        if len(image) != length or sha(image) != expected:
            raise ValueError("received image hash differs")
        images.append(image)
        pos += length
    if pos != len(body) or len(images) != 4:
        raise ValueError("request framing error")
    return header, images


def infer(body, callback):
    header, images = parse_body(body)
    content = [{"type": "image_url", "image_url": {
        "url": "data:image/jpeg;base64," + base64.b64encode(data).decode()}} for data in images]
    content.append({"type": "text", "text": PROMPT + "\n" + header["question"] + "\n" +
                    "\n".join(header["options"]) + "\nAnswer:"})
    payload = dict(model="minicpm", messages=[{"role": "user", "content": content}],
                   temperature=0, seed=20261105, max_tokens=12, stream=True, cache_prompt=False)
    conn = http.client.HTTPConnection("127.0.0.1", MODEL_PORT, timeout=30)
    started = time.monotonic()
    output = []
    try:
        conn.request("POST", "/v1/chat/completions", json.dumps(payload), {"Content-Type": "application/json"})
        response = conn.getresponse()
        if response.status != 200:
            raise RuntimeError(f"local inference HTTP status {response.status}")
        while line := response.readline():
            if not line.startswith(b"data: "):
                continue
            if line.strip() == b"data: [DONE]":
                break
            delta = json.loads(line[6:]).get("choices", [])
            if delta and (text := delta[0]["delta"].get("content")):
                stamp = time.monotonic()
                output.append((stamp, text))
                callback(dict(kind="text", text=text))
        return dict(infer_start=started, infer_end=time.monotonic(), tokens=output)
    finally:
        conn.close()


class CapPacer(ProbePacer):
    origin = None
    rates = None
    dwell = None
    tokens = 2400.0
    cap_at = None
    cap_calls = 0

    def budget(self, when):
        elapsed = max(0, when - self.origin)
        if not self.dwell:
            return elapsed * self.rates[0] * 125000
        slots = int(elapsed / self.dwell)
        complete = (slots // 2) * sum(self.rates)
        if slots % 2:
            complete += self.rates[0]
        partial = elapsed - slots * self.dwell
        return (complete * self.dwell + partial * self.rates[slots % 2]) * 125000

    def available(self, now):
        if self.origin is None or self.rates is None or now < self.origin:
            return None
        slot = int((now - self.origin) / self.dwell) if self.dwell else 0
        rate = self.rates[slot % len(self.rates)] * 125000
        previous = self.cap_at if self.cap_at is not None else self.origin
        self.tokens = min(2400, self.tokens + max(0, self.budget(now) - self.budget(previous)))
        self.cap_at = now
        return rate

    def next_send_time(self, now):
        native = super().next_send_time(now)
        rate = self.available(now)
        if rate is None or self.tokens >= 1200:
            return native
        self.cap_calls += 1
        return max(now + (1200-self.tokens) / rate, native or now)

    def update_after_send(self, now):
        super().update_after_send(now)
        if self.available(now) is not None:
            self.tokens = max(0, self.tokens-1200)


def dump(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, separators=(",", ":")))
    tmp.replace(path)


async def server(work, algorithm, logger):
    state = {}
    tasks = set()

    def factory(*args, **kwargs):
        conn = Protocol(*args, **kwargs)
        raw = bytearray()
        arrivals = []
        started = False

        def on_data(proto, event):
            nonlocal started
            if started:
                return
            raw.extend(event.data)
            stamp = time.monotonic()
            if event.data:
                arrivals.append((stamp, len(raw)))
            if not event.end_stream:
                return
            started = True
            sid = event.stream_id
            state.update(upload_done=stamp, upload_bytes=len(raw), sha256=sha(raw), arrivals=arrivals)
            (work / "upload_done").touch()

            def reply(item, final=False):
                proto._quic.send_stream_data(sid, json.dumps(item).encode() + b"\n", end_stream=final)
                proto.transmit()

            async def execute():
                loop = asyncio.get_running_loop()
                try:
                    state.update(await asyncio.to_thread(infer, bytes(raw),
                                                         lambda item: loop.call_soon_threadsafe(reply, item)))
                    reply(dict(kind="done"), True)
                except Exception as error:
                    state["error"] = type(error).__name__ + ": " + str(error)
                    reply(dict(kind="error", error=state["error"]), True)
                finally:
                    dump(work / "server.json", state)

            task = asyncio.create_task(execute())
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        conn.on_data = on_data
        return conn

    service = await serve(SERVER, PORT, configuration=quic_config(work, algorithm, logger, False),
                          create_protocol=factory)
    (work / "server_ready").touch()
    try:
        await wait_path(work / "stop", timeout=60)
        if tasks:
            await asyncio.gather(*tasks)
    finally:
        service.close()


async def client(work, algorithm, logger, body, profile):
    recovery.QuicPacketPacer = CapPacer
    handshake_start = time.monotonic()
    session = connect(SERVER, PORT, configuration=quic_config(work, algorithm, logger, True),
                      create_protocol=Protocol)
    conn = await asyncio.wait_for(session.__aenter__(), 8)
    connected = time.monotonic()
    try:
        (work / "client_ready").touch()
        target = float(await wait_path(work / "start"))
        await wait_until(target)
        pacer = conn._quic._loss._pacer
        cap = profile.get("cap")
        if cap:
            pacer.origin = target
            pacer.dwell = profile.get("dwell", 0) / 1000
            rates = profile.get("rate", [])
            pacer.rates = [8] if cap == "fixed8" else rates if cap == "matched" else rates[::-1]
        conn.track = True
        stream = conn._quic.get_next_available_stream_id()
        conn.stream = stream
        pending = bytearray()
        items = []
        finished = asyncio.Event()

        def incoming(_, event):
            if event.stream_id != stream:
                return
            pending.extend(event.data)
            while (newline := pending.find(b"\n")) >= 0:
                item = json.loads(pending[:newline])
                del pending[:newline+1]
                items.append((time.monotonic(), item))
            if event.end_stream:
                finished.set()

        conn.on_data = incoming
        started = time.monotonic()
        conn._quic.send_stream_data(stream, body, end_stream=True)
        conn.transmit()
        status = "complete"
        try:
            await asyncio.wait_for(finished.wait(), 8)
        except TimeoutError:
            status = "timeout"
        if any(item["kind"] == "error" for _, item in items):
            status = "model_error"
        visible = [(stamp, item["text"]) for stamp, item in items if item["kind"] == "text"]
        response = "".join(text for _, text in visible)
        prediction = re.fullmatch(r"\s*([ABCD])[.)。]?\s*", response, flags=re.I)
        result = dict(status=status, handshake_ms=(connected-handshake_start)*1000, start=started,
                      first=next((t for t, s in visible if s.strip()), None),
                      last=max((t for t, _ in visible), default=None), finish=time.monotonic(),
                      prediction=prediction.group(1).upper() if prediction else None,
                      answer_chars=len(response), offered_bytes=len(body), sha256=sha(body),
                      samples=conn.samples, pacing_waits=conn.waits, pacing_wakes=conn.wakes,
                      service_cap_waits=pacer.cap_calls)
        dump(work / "client.json", result)
    finally:
        try:
            await asyncio.wait_for(session.__aexit__(None, None, None), 2)
        except TimeoutError:
            conn._transport.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("role", choices=["server", "client"])
    p.add_argument("work", type=Path)
    args = p.parse_args()
    profile = json.loads((args.work / "profile.json").read_text())
    logger = QuicLogger()
    algorithm = profile.get("algorithm", "cubic")
    try:
        if args.role == "server":
            asyncio.run(server(args.work, algorithm, logger))
        else:
            asyncio.run(client(args.work, algorithm, logger, (args.work / "input.bin").read_bytes(), profile))
    finally:
        with gzip.open(args.work / (args.role + ".qlog.json.gz"), "wt") as handle:
            json.dump(logger.to_dict(), handle, separators=(",", ":"))


if __name__ == "__main__":
    main()
