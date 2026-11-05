"""Diagnostic endpoint. Observe aioquic 1.2.0's actual loss declarations."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aioquic.quic import recovery
from endpoint import main

CASES = {"cubic": (3, 9/8), "cubic_k64": (64, 9/8),
         "cubic_t2": (3, 2), "cubic_k64_t2": (64, 2)}


def observe():
    recovery_cls = recovery.QuicPacketRecovery
    detect_loss = recovery_cls._detect_loss
    on_packets_lost = recovery_cls._on_packets_lost
    events = []

    def detect(self, *, now, space):
        self.qa_detecting = True
        try:
            return detect_loss(self, now=now, space=space)
        finally:
            self.qa_detecting = False

    def on_loss(self, *, now, packets, space):
        packets = list(packets)
        if not packets:
            return on_packets_lost(self, now=now, packets=packets, space=space)
        before = self.congestion_window
        latest = max(self._rtt_latest, self._rtt_smoothed) if self._rtt_initialized else self._rtt_initial
        delay = recovery.K_TIME_THRESHOLD * latest
        decisions = []
        for packet in packets:
            gap = space.largest_acked_packet - packet.packet_number
            age = now - packet.sent_time
            by_number = gap >= recovery.K_PACKET_THRESHOLD
            by_time = packet.sent_time <= now-delay
            if not getattr(self, "qa_detecting", False):
                reason = "rescheduled"
            else:
                reason = "both" if by_number and by_time else "number" if by_number else "time"
                if not by_number and not by_time:
                    raise AssertionError("unexplained native loss")
            decisions.append((packet.packet_number,packet.epoch.name,reason,gap,age,packet.in_flight))
        result = on_packets_lost(self, now=now, packets=packets, space=space)
        events.append((now,before,self.congestion_window,latest,decisions))
        return result

    recovery_cls._detect_loss = detect
    recovery_cls._on_packets_lost = on_loss
    return events


if __name__ == "__main__":
    role, work, _, variant, _ = sys.argv[1:]
    sys.argv[4] = "cubic"
    if role == "client":
        recovery.K_PACKET_THRESHOLD, recovery.K_TIME_THRESHOLD = CASES[variant]
        history = observe()
        try:
            main()
        finally:
            (Path(work)/"loss_decisions.json").write_text(json.dumps(history,separators=(",",":")))
    else:
        main()
