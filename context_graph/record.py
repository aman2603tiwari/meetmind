"""Watch Meetily for a live recording and hand back the finished meeting.

`meetmind record --link <url>` uses this: you tag a meeting, start recording in
Meetily, and this watches the local DB — detecting the new meeting, waiting until
its transcript stops growing (the call ended), then returning it for ingest. No
control over Meetily is needed; it only reads the DB.

The loop takes injectable `time_fn`/`sleep_fn`/`stop_flag` so it can be unit
tested without real waiting.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from typing import Callable, Optional

from . import meetily


@dataclass
class Recording:
    meeting_id: str
    title: str
    created_at: str


def watch_for_recording(
    *,
    db_path: str | None = None,
    poll: float = 5.0,
    idle: float = 30.0,
    baseline: Optional[set] = None,
    status: Optional[Callable[[str], None]] = None,
    stop_flag: Optional[Callable[[], bool]] = None,
    time_fn: Callable[[], float] = _time.monotonic,
    sleep_fn: Callable[[float], None] = _time.sleep,
) -> Optional[Recording]:
    """Block until a new Meetily meeting appears and its transcript stabilises.

    Returns the finished Recording, or None if stopped before any new meeting
    was detected. A recording is considered "ended" when its transcript size is
    unchanged for `idle` seconds. `stop_flag()` returning True (e.g. Ctrl+C)
    finalises immediately with whatever transcript exists so far.
    """
    say = status or (lambda _m: None)
    stopped = stop_flag or (lambda: False)
    if baseline is None:
        baseline = meetily.meeting_ids(db_path)

    # --- Phase A: wait for a new meeting to appear ---
    say(f"armed — waiting for a new Meetily recording ({len(baseline)} existing)")
    target: Optional[Recording] = None
    while target is None:
        if stopped():
            say("stopped before any recording started")
            return None
        for m in meetily.list_meetings(db_path=db_path, limit=50):
            if m.id not in baseline:
                target = Recording(m.id, m.title, m.created_at)
                break
        if target is None:
            sleep_fn(poll)
    say(f"recording detected: {target.title!r} — capturing…")

    # --- Phase B: wait until the transcript stops growing ---
    last_size = -1
    last_change = time_fn()
    while True:
        size = meetily.transcript_size(target.meeting_id, db_path)
        now = time_fn()
        if size != last_size:
            last_size = size
            last_change = now
            say(f"capturing… {size} chars")
        if stopped():
            say("finalising now (stopped)")
            break
        if size > 0 and (now - last_change) >= idle:
            say(f"recording ended (no new speech for {idle:.0f}s) — {size} chars")
            break
        sleep_fn(poll)

    return target
