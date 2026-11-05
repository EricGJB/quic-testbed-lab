"""Local QUIC/TLS and TCP/TLS endpoints, using a shared Linux monotonic clock."""

import argparse
import asyncio
import gzip
import hashlib
import json
import ssl
import struct
import time
from pathlib import Path

from aioquic.asyncio import QuicConnectionProtocol, connect, serve
from aioquic.quic import recovery
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import DatagramFrameReceived, StreamDataReceived
from aioquic.quic.logger import QuicLogger

CLIENT = "10.206.0.2"
SERVER = "10.207.0.2"
PORT = 9443
BLOCK = struct.Struct("!IBHH")
PAYLOAD = 1120
CHUNK = bytes([42]) * 65536
STEP = 0.005


def digest(data):
    return hashlib.sha256(data).hexdigest()


async def wait_path(path, timeout=15):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(path.name)
        await asyncio.sleep(0.005)
    return path.read_text()


async def wait_until(when):
    await asyncio.sleep(max(0, when - time.monotonic()))


class ProbePacer(recovery.QuicPacketPacer):
    waits = None
    small = False

    def update_rate(self, congestion_window, smoothed_rtt):
        super().update_rate(congestion_window, smoothed_rtt)
        if self.small:
            self.bucket_max = min(self.bucket_max, 2 * self.packet_time)
            self.bucket_time = min(self.bucket_time, self.bucket_max)

    def next_send_time(self, now):
        when = super().next_send_time(now)
        if when is not None and self.waits is not None:
            self.waits.append((now, when, self.packet_time, self.bucket_max))
        return when


class Protocol(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stream = None
        self.samples = []
        self.waits = []
        self.wakes = []
        self.last_sample = 0.0
        self.track = False
        self.on_data = None
        self.on_datagram = None
        self._quic._loss._pacer.waits = self.waits

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived) and self.on_data:
            self.on_data(self, event)
        if isinstance(event, DatagramFrameReceived) and self.on_datagram:
            self.on_datagram(event.data)

    def _handle_timer(self):
        if self.track and self._timer_at == self._quic._pacing_at:
            self.wakes.append((self._timer_at, self._loop.time()))
        super()._handle_timer()

    def transmit(self):
        super().transmit()
        if not self.track:
            return
        now = self._loop.time()
        if now - self.last_sample < STEP:
            return
        self.last_sample = now
        loss = self._quic._loss
        pacer = loss._pacer
        stream = self._quic._streams.get(self.stream)
        pending = bool(stream and len(stream.sender._pending)) or bool(self._quic._datagrams_pending)
        credit = loss.congestion_window - loss.bytes_in_flight
        self.samples.append((now, loss.congestion_window, loss.bytes_in_flight,
                             loss._rtt_smoothed, pacer.packet_time, pending,
                             pending and credit < self._quic._max_datagram_size,
                             loss._pto_count, self._quic._pacing_at))


class Receiver:
    def __init__(self, work, mode):
        self.work = work
        self.mode = mode
        self.streams = {}
        self.manifest = {}
        self.packets = {}
        self.bad = 0
        self.bulk = []
        self.last = 0.0
        self.total = 0
        self.iframe_verified = False

    def add(self, data):
        self.total += len(data)
        self.bad += len(data) - data.count(42)
        now = time.monotonic()
        if now - self.last >= STEP:
            self.bulk.append((now, self.total))
            self.last = now

    def stream(self, conn, event):
        if self.mode == "bulk":
            self.add(event.data)
            return
        buffer = self.streams.setdefault(event.stream_id, bytearray())
        buffer.extend(event.data)
        if not event.end_stream:
            return
        size = struct.unpack_from("!I", buffer)[0]
        info = json.loads(buffer[4:4 + size])
        iframe = memoryview(buffer)[4 + size:]
        if digest(iframe) != info["iframe_sha256"]:
            raise ValueError("I-frame stream SHA-256 mismatch")
        self.manifest = {row["id"]: row for row in info["frames"]}
        self.iframe_verified = True
        conn._quic.send_stream_data(event.stream_id, b"verified", end_stream=True)
        del self.streams[event.stream_id]

    def datagram(self, data):
        fid, block, index, count = BLOCK.unpack_from(data)
        if fid not in self.manifest or block >= 10 or not 0 <= index < count:
            raise ValueError("invalid GRACE fragment")
        fragments = self.packets.setdefault((fid, block), {})
        fragments.setdefault(index, (time.monotonic(), data[BLOCK.size:]))

    def result(self):
        if self.mode == "bulk":
            self.bulk.append((time.monotonic(), self.total))
            return dict(received=self.bulk, corrupt_bytes=self.bad, total=self.total)
        origin = float((self.work / "start").read_text())
        rows = []
        for fid, frame in self.manifest.items():
            rx = []
            for block, spec in enumerate(frame["blocks"]):
                fragments = self.packets.get((fid, block), {})
                delivered = sorted((index, (stamp - origin) * 1000, len(body))
                                   for index, (stamp, body) in fragments.items())
                complete = None
                if len(fragments) == spec["count"]:
                    joined = b"".join(fragments[i][1] for i in range(spec["count"]))
                    if digest(joined) != spec["sha256"]:
                        raise ValueError("received block SHA-256 mismatch")
                    complete = max(row[1] for row in delivered)
                rx.append(dict(fragments=delivered, complete_ms=complete))
            rows.append(dict(id=fid, send_ms=frame["send_ms"], blocks=rx))
        return dict(iframe_verified=self.iframe_verified, frames=rows)


def quic_config(work, algorithm, logger, is_client):
    config = QuicConfiguration(
        is_client=is_client, alpn_protocols=["testbed-quic/1"],
        congestion_control_algorithm=algorithm, max_data=16 * 1024 * 1024,
        max_stream_data=16 * 1024 * 1024, max_datagram_frame_size=1200,
        max_datagram_size=1200, idle_timeout=20, quic_logger=logger,
    )
    if is_client:
        config.server_name = "localhost"
        config.load_verify_locations(cafile=str(work.parent / "cert.pem"))
    else:
        config.load_cert_chain(work.parent / "cert.pem", work.parent / "key.pem")
    return config


async def receive(work, mode, algorithm, logger):
    receiver = Receiver(work, mode)
    if algorithm == "tcp":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(work.parent / "cert.pem", work.parent / "key.pem")

        async def tcp_data(reader, writer):
            try:
                while data := await reader.read(65536):
                    receiver.add(data)
            finally:
                writer.close()

        server = await asyncio.start_server(tcp_data, SERVER, PORT, ssl=context)
    else:
        def factory(*args, **kwargs):
            conn = Protocol(*args, **kwargs)
            conn.on_data = receiver.stream
            conn.on_datagram = receiver.datagram
            return conn
        server = await serve(SERVER, PORT, configuration=quic_config(work, algorithm, logger, False),
                             create_protocol=factory)
    (work / "server_ready").touch()
    try:
        await wait_path(work / "stop", timeout=35)
        result = receiver.result()
        (work / "received.json").write_text(json.dumps(result))
    finally:
        server.close()


async def bulk_quic(conn, start, duration):
    stream = conn._quic.get_next_available_stream_id()
    conn.stream = stream
    conn.track = True
    offered = 0
    while time.monotonic() < start + duration:
        current = conn._quic._streams.get(stream)
        buffered = len(current.sender._buffer) if current else 0
        while buffered < 512 * 1024:
            conn._quic.send_stream_data(stream, CHUNK)
            offered += len(CHUNK)
            buffered += len(CHUNK)
        conn.transmit()
        await asyncio.sleep(0.002)
    conn._quic.send_stream_data(stream, b"", end_stream=True)
    conn.transmit()
    return offered


async def grace_quic(conn, work):
    manifest = json.loads((work.parent / "media.json").read_text())
    raw = (work.parent / "media.bin").read_bytes()
    iframe = (work.parent / "iframes.bin").read_bytes()
    config = json.dumps(manifest).encode()
    stream = conn._quic.get_next_available_stream_id()
    verified = asyncio.Event()

    def on_ack(_, event):
        if event.stream_id == stream and event.data == b"verified" and event.end_stream:
            verified.set()

    conn.on_data = on_ack
    conn._quic.send_stream_data(stream, struct.pack("!I", len(config)) + config + iframe, end_stream=True)
    conn.transmit()
    await asyncio.wait_for(verified.wait(), 10)
    (work / "client_ready").touch()
    start = float(await wait_path(work / "start"))
    offered = []
    conn.track = True
    for frame in manifest["frames"]:
        await wait_until(start + frame["send_ms"] / 1000)
        for index, block in enumerate(frame["blocks"]):
            data = raw[block["offset"]:block["offset"] + block["length"]]
            for part in range(block["count"]):
                fragment = data[part * PAYLOAD:(part + 1) * PAYLOAD]
                conn._quic.send_datagram_frame(BLOCK.pack(frame["id"], index, part, block["count"]) + fragment)
        offered.append((frame["id"], (time.monotonic() - start) * 1000))
        conn.transmit()
    return offered


async def send(work, mode, algorithm, duration, logger):
    if algorithm == "tcp":
        context = ssl.create_default_context(cafile=str(work.parent / "cert.pem"))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        _, writer = await asyncio.open_connection(SERVER, PORT, ssl=context, server_hostname="localhost")
        writer.get_extra_info("socket").setsockopt(6, 13, b"cubic")
        writer.transport.set_write_buffer_limits(high=131072, low=65536)
        (work / "client_ready").touch()
        start = float(await wait_path(work / "start"))
        await wait_until(start)
        offered = 0
        try:
            async with asyncio.timeout_at(start + duration):
                while True:
                    writer.write(CHUNK)
                    offered += len(CHUNK)
                    await writer.drain()
        except TimeoutError:
            pass
        (work / "sent.json").write_text(json.dumps(dict(offered=offered)))
        await wait_path(work / "stop", timeout=10)
        writer.close()
        return
    session = connect(SERVER, PORT, configuration=quic_config(work, algorithm, logger, True),
                      create_protocol=Protocol)
    conn = await session.__aenter__()
    try:
        if mode == "bulk":
            (work / "client_ready").touch()
            start = float(await wait_path(work / "start"))
            await wait_until(start)
            offered = await bulk_quic(conn, start, duration)
        else:
            offered = await grace_quic(conn, work)
        await wait_path(work / "stop", timeout=15)
        result = dict(offered=offered, samples=conn.samples, pacing_waits=conn.waits, pacing_wakes=conn.wakes)
        (work / "sent.json").write_text(json.dumps(result))
    finally:
        # At this point the measurement window has closed. Do not wait for an
        # arbitrarily long closing PTO on the impaired link.
        try:
            await asyncio.wait_for(session.__aexit__(None, None, None), timeout=2)
        except TimeoutError:
            conn._transport.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["server", "client"])
    parser.add_argument("work", type=Path)
    parser.add_argument("mode", choices=["bulk", "grace"])
    parser.add_argument("algorithm")
    parser.add_argument("duration", type=float)
    parser.add_argument("--no-qlog", action="store_true")
    args = parser.parse_args()
    ProbePacer.small = args.algorithm == "cubic_small_burst" and args.role == "client"
    recovery.QuicPacketPacer = ProbePacer
    if args.algorithm == "cubic_pkt12" and args.role == "client":
        recovery.K_PACKET_THRESHOLD = 12
    algorithm = args.algorithm.removesuffix("_small_burst").removesuffix("_pkt12")
    logger = None if args.no_qlog or algorithm == "tcp" else QuicLogger()
    if args.role == "client":
        asyncio.run(send(args.work, args.mode, algorithm, args.duration, logger))
    else:
        asyncio.run(receive(args.work, args.mode, algorithm, logger))
    if logger is not None:
        with gzip.open(args.work / (args.role + ".qlog.json.gz"), "wt") as handle:
            json.dump(logger.to_dict(), handle, separators=(",", ":"))


if __name__ == "__main__":
    main()
