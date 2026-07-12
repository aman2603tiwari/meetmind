"""Read meeting transcripts directly from Meetily's local SQLite database.

Meetily (github.com/Zackriya-Solutions/meetily) captures system+mic audio and
transcribes locally, storing everything in SQLite — there is no file export — so
we read that DB read-only. This covers the recording -> transcript step; our
pipeline then turns the transcript into the context graph.

DB location (Tauri identifier `com.meetily.ai`):
  Windows : %APPDATA%\com.meetily.ai\meeting_minutes.sqlite
  macOS   : ~/Library/Application Support/com.meetily.ai/meeting_minutes.sqlite
  Linux   : ~/.local/share/com.meetily.ai/meeting_minutes.sqlite

Schema used:
  meetings(id TEXT, title TEXT, created_at TEXT, ...)
  transcripts(meeting_id TEXT, transcript TEXT, timestamp TEXT,
              speaker TEXT('mic'|'system'), audio_start_time REAL, ...)
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

APP_IDENTIFIER = "com.meetily.ai"
DB_FILENAME = "meeting_minutes.sqlite"
LEGACY_DB_FILENAME = "meeting_minutes.db"

# 'mic' = the local user, 'system' = everyone else on the call
_SPEAKER_LABEL = {"mic": "You", "system": "Others"}


@dataclass
class MeetilyMeeting:
    id: str
    title: str
    created_at: str


def default_db_path() -> Path:
    """Resolve Meetily's SQLite path for the current OS."""
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / APP_IDENTIFIER / DB_FILENAME


def _resolve_db(db_path: str | None) -> Path:
    if db_path:
        p = Path(db_path)
    else:
        p = default_db_path()
        if not p.exists():
            legacy = p.with_name(LEGACY_DB_FILENAME)
            if legacy.exists():
                p = legacy
    if not p.exists():
        raise FileNotFoundError(
            "Meetily is not set up — it's the (free, local) app that RECORDS your meetings;\n"
            "meetmind reads the transcripts it produces.\n\n"
            "  1. Install Meetily: https://github.com/Zackriya-Solutions/meetily/releases\n"
            "  2. Open it, allow mic + system audio, and record a meeting.\n"
            "  3. Then run:  meetmind ingest --meetily\n\n"
            "Run 'meetmind setup' for a full checklist.\n"
            "(No meeting to record? You can still use: meetmind ingest --paste \"...\")\n"
            f"Looked for the database at: {p}\n"
            "If Meetily is installed elsewhere, pass --meetily-db PATH."
        )
    return p


def _connect(db_path: str | None) -> sqlite3.Connection:
    p = _resolve_db(db_path)
    # read-only URI so we never risk mutating Meetily's data
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_meetings(db_path: str | None = None, limit: int = 20) -> List[MeetilyMeeting]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, title, created_at FROM meetings ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [MeetilyMeeting(r["id"], r["title"] or "(untitled)", r["created_at"] or "") for r in rows]


def list_meetings_since(since: str | None, db_path: str | None = None) -> List[MeetilyMeeting]:
    """Meetings with created_at >= `since` (ISO string/date), oldest first.

    `since` None returns all meetings. Comparison is lexicographic, which is
    correct for ISO-8601 timestamps ('2026-07-12' <= '2026-07-12T14:00:00').
    """
    conn = _connect(db_path)
    try:
        if since:
            rows = conn.execute(
                "SELECT id, title, created_at FROM meetings "
                "WHERE created_at >= ? ORDER BY created_at ASC",
                (since,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, created_at FROM meetings ORDER BY created_at ASC"
            ).fetchall()
    finally:
        conn.close()
    return [MeetilyMeeting(r["id"], r["title"] or "(untitled)", r["created_at"] or "") for r in rows]


def meeting_ids(db_path: str | None = None) -> set:
    """Set of all meeting ids currently in the DB (for baseline snapshots)."""
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT id FROM meetings").fetchall()
    finally:
        conn.close()
    return {r["id"] for r in rows}


def transcript_size(meeting_id: str, db_path: str | None = None) -> int:
    """Total transcript character count for a meeting (0 if none yet).

    Used to tell whether a recording is still growing or has stopped.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(transcript)), 0) AS n "
            "FROM transcripts WHERE meeting_id = ?",
            (meeting_id,),
        ).fetchone()
    finally:
        conn.close()
    return int(row["n"] or 0)


def _fetch_meeting_row(conn: sqlite3.Connection, meeting_id: Optional[str]) -> sqlite3.Row:
    if meeting_id:
        row = conn.execute(
            "SELECT id, title, created_at FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no Meetily meeting with id '{meeting_id}'")
        return row
    row = conn.execute(
        "SELECT id, title, created_at FROM meetings ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValueError("Meetily database has no meetings yet")
    return row


def get_transcript(
    meeting_id: str | None = None, db_path: str | None = None
) -> tuple[str, MeetilyMeeting]:
    """Return (transcript_text, meeting). Latest meeting if meeting_id is None."""
    conn = _connect(db_path)
    try:
        m = _fetch_meeting_row(conn, meeting_id)
        # order by audio offset when present, else by textual timestamp
        segs = conn.execute(
            """
            SELECT transcript, speaker
            FROM transcripts
            WHERE meeting_id = ?
            ORDER BY COALESCE(audio_start_time, 1e18), timestamp
            """,
            (m["id"],),
        ).fetchall()
    finally:
        conn.close()

    lines: List[str] = []
    for seg in segs:
        text = (seg["transcript"] or "").strip()
        if not text:
            continue
        speaker = seg["speaker"]
        label = _SPEAKER_LABEL.get(speaker) if speaker else None
        lines.append(f"{label}: {text}" if label else text)

    transcript = "\n".join(lines)
    meeting = MeetilyMeeting(m["id"], m["title"] or "(untitled)", m["created_at"] or "")
    if not transcript.strip():
        raise ValueError(f"Meetily meeting '{meeting.id}' has no transcript segments yet")
    return transcript, meeting
