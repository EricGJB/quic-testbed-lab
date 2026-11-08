"""Transparent UDP relay with an explicit, FIFO, piecewise-constant service budget."""

import argparse
import bisect
import csv
import json
import select
import socket
import time
from collections import deque
from pathlib import Path

CLIENT = ("10.206.0.2", 4433)
SERVER = ("10.207.0.2", 4433)


def open_port(host):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4*1024*1024)
    sock.bind((host, 4433))
    sock.setblocking(False)
    return sock


def main(work, origin, duration):
    plan = json.loads((work / "plan.json").read_text())
    offsets, rates = zip(*plan["capacity"])
    bounds = [origin + t for t in offsets]
    end = origin + duration + 200_000
    client = open_port("10.206.0.1")
    server = open_port("10.207.0.1")
    queue = deque()
    prior_due = 0
    backlog = 0

    def finish(start, bits):
        index = max(0, bisect.bisect_right(bounds, start)-1)
        while index+1 < len(bounds):
            span = bounds[index+1] - start
            if bits <= span*rates[index]:
                break
            bits -= span*rates[index]
            index += 1
            start = bounds[index]
        return start + bits/rates[index]

    with (work / "wire.csv").open("w", newline="") as stream:
        log = csv.writer(stream)
        log.writerow(("event", "us", "bytes", "queue", "due_us"))
        while (now := time.monotonic_ns()//1000) < end:
            while queue and queue[0][0] <= now:
                due, payload, ip_bytes = queue.popleft()
                server.sendto(payload, SERVER)
                backlog -= ip_bytes
                log.writerow(("out", now, ip_bytes, backlog, round(due)))
                now = time.monotonic_ns()//1000
            deadline = min(end, queue[0][0]) if queue else end
            readable, _, _ = select.select((client, server), (), (), max(0, deadline-now)/1e6)
            for sock in readable:
                for _ in range(32):
                    try:
                        payload, _ = sock.recvfrom(65536)
                    except BlockingIOError:
                        break
                    now = time.monotonic_ns()//1000
                    if sock is server:
                        client.sendto(payload, CLIENT)
                        continue
                    ip_bytes = len(payload) + 28
                    if len(queue) >= 256:
                        log.writerow(("drop", now, ip_bytes, backlog, 0))
                        continue
                    prior_due = finish(max(now, prior_due), ip_bytes*8)
                    backlog += ip_bytes
                    queue.append((prior_due, payload, ip_bytes))
                    log.writerow(("in", now, ip_bytes, backlog, round(prior_due)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path)
    parser.add_argument("origin", type=int)
    parser.add_argument("duration", type=int)
    args = parser.parse_args()
    main(args.work, args.origin, args.duration)
