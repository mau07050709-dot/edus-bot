"""Planificación secuencial y límites para la vigilancia."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta


REAL_WATCH_INTERVAL_SECONDS = 120
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 3600
MAX_QUERIES = 120
MAX_DURATION = timedelta(hours=24)


def watch_window(now, start, end):
    starts = now
    if start:
        starts = datetime.combine(now.date(), time.fromisoformat(start), now.tzinfo)
        if starts <= now:
            # Un inicio dentro del minuto seleccionado empieza en el momento actual.
            starts = now if now - starts < timedelta(minutes=1) else starts + timedelta(days=1)
    ends = starts + MAX_DURATION
    if end:
        cutoff = datetime.combine(starts.date(), time.fromisoformat(end), now.tzinfo)
        if cutoff <= starts:
            cutoff += timedelta(days=1)
        ends = min(ends, cutoff)
    return starts, ends


@dataclass
class WatchCycles:
    interval: int | None
    limit: int | None
    end: datetime | None
    activated: float
    count: int = 0
    slots: int = 0
    in_progress: bool = False
    stopped: bool = False
    next_at: float = 0
    last: datetime | None = None

    def begin(self, now, tick):
        if self.stopped or self.in_progress or (self.end is not None and now >= self.end) or tick < self.next_at:
            return False
        self.in_progress = True
        if self.interval is not None:
            self.next_at = tick + self.interval
        return True

    def pause(self):
        self.in_progress = False

    def finish(self, timestamp, slots, actual_start=None, *, finished_tick=None):
        if not self.in_progress or self.stopped:
            return
        self.in_progress = False
        self.count += 1
        self.slots = slots
        self.last = timestamp
        if self.interval is not None:
            if finished_tick is not None:
                self.next_at = finished_tick + self.interval
            elif actual_start is not None:
                self.next_at = max(self.next_at, actual_start + self.interval)
        if self.interval is None or slots or (self.limit is not None and self.count >= self.limit):
            self.stop()

    def stop(self):
        self.stopped = True
        self.in_progress = False
