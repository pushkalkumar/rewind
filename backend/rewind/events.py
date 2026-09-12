"""Event sink: components report progress here; the runner persists and streams it."""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from .models import Event


class EventSink:
    def __init__(self, on_event: Optional[Callable[[Event], None]] = None):
        self.start = time.time()
        self.seq = 0
        self.events: list[Event] = []
        self.on_event = on_event

    def emit(self, type: str, **data: Any) -> Event:
        self.seq += 1
        ev = Event(seq=self.seq, t=round(time.time() - self.start, 3), type=type, data=data)
        self.events.append(ev)
        if self.on_event:
            self.on_event(ev)
        return ev


class PrintSink(EventSink):
    def __init__(self):
        super().__init__(on_event=lambda ev: print(f"[{ev.t:7.2f}s] {ev.type:<16} {ev.data}"))
