"""
Reads the app's own log file (settings.LOG_DIR/school360.log) for the
"System logs" page on the platform. Only the end of the file is read, so a big
log never slows the page down.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from django.conf import settings

LOG_FILE_NAME = "school360.log"
READ_BYTES = 2 * 1024 * 1024          # last 2 MB of the current + previous file

# "2026-11-01 10:15:02,123 ERROR django.request: Internal Server Error: /x/"
LINE_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL) "
    r"(?P<logger>[\w.\-]+): (?P<message>.*)$"
)
PATH_RE = re.compile(r"(?:Internal Server Error|Not Found|Forbidden|Bad Request|Method Not Allowed[^:]*): (\S+)")

LEVELS = ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]


@dataclass
class LogEntry:
    time: str
    level: str
    logger: str
    message: str
    details: list = field(default_factory=list)   # traceback lines

    @property
    def is_cross_school(self):
        return "Cross-school access" in self.message

    @property
    def page(self):
        m = PATH_RE.search(self.message)
        return m.group(1) if m else ""

    @property
    def error_line(self):
        """Last line of a traceback = the actual error, e.g. 'KeyError: x'."""
        for line in reversed(self.details):
            if line.strip():
                return line.strip()
        return ""

    @property
    def text(self):
        return "\n".join([f"{self.time} {self.level} {self.logger}: {self.message}", *self.details])


def log_path():
    return os.path.join(getattr(settings, "LOG_DIR", ""), LOG_FILE_NAME)


def _tail(path, max_bytes):
    if not os.path.isfile(path):
        return ""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()                 # drop the half line we landed in
        return fh.read().decode("utf-8", errors="replace")


def read_raw():
    """Text of the previous file (.1) followed by the current one, at most READ_BYTES."""
    path = log_path()
    current = _tail(path, READ_BYTES)
    rest = READ_BYTES - len(current.encode("utf-8", errors="replace"))
    older = _tail(path + ".1", rest) if rest > 0 else ""
    return older + current


def parse(text):
    entries = []
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if m:
            entries.append(LogEntry(m["time"], m["level"], m["logger"], m["message"]))
        elif entries:
            entries[-1].details.append(line)
    return entries


def read_entries(*, level="WARNING", query="", cross_school_only=False, limit=300):
    """Newest first. level = lowest level to show ("ERROR" shows ERROR + CRITICAL)."""
    entries = parse(read_raw())
    if level in LEVELS:
        allowed = set(LEVELS[: LEVELS.index(level) + 1])
        entries = [e for e in entries if e.level in allowed]
    if cross_school_only:
        entries = [e for e in entries if e.is_cross_school]
    if query:
        q = query.lower()
        entries = [e for e in entries if q in e.text.lower()]
    entries.reverse()
    return entries[:limit]


def summary():
    """Counts for the page header (from the part of the log that is read)."""
    entries = parse(read_raw())
    return {
        "errors": sum(1 for e in entries if e.level in ("ERROR", "CRITICAL") and not e.is_cross_school),
        "warnings": sum(1 for e in entries if e.level == "WARNING"),
        "cross_school": sum(1 for e in entries if e.is_cross_school),
        "first_time": entries[0].time if entries else "",
        "last_time": entries[-1].time if entries else "",
    }
