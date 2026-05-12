from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ScraperState:
    """Shared state between the scraper background thread and the UI.

    The scraper thread is the only writer (except for control flags set
    via methods below). The UI thread reads via NiceGUI bindings/timers.
    Primitive field reads are safe in CPython without a lock.
    """

    status: str = "idle"               # idle | scraping | paused | done | error
    total: int = 0                     # total posts to process this run
    completed: int = 0                 # posts completed this run
    current_message: str = ""
    log_lines: list[str] = field(default_factory=list)
    pdf_path: str = ""

    _pause_event: threading.Event = field(default_factory=threading.Event)
    _stop_event: threading.Event = field(default_factory=threading.Event)

    MAX_LOG_LINES: int = 500

    @property
    def progress(self) -> float:
        return self.completed / self.total if self.total else 0.0

    def log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.log_lines.append(line)
        if len(self.log_lines) > self.MAX_LOG_LINES:
            self.log_lines = self.log_lines[-self.MAX_LOG_LINES:]
        self.current_message = msg

    def request_pause(self) -> None:
        self._pause_event.set()

    def clear_pause(self) -> None:
        self._pause_event.clear()

    def pause_requested(self) -> bool:
        return self._pause_event.is_set()

    def reset_for_run(self) -> None:
        self._pause_event.clear()
        self._stop_event.clear()
        self.total = 0
        self.completed = 0
        self.current_message = ""
        self.pdf_path = ""
