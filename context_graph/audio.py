"""Audio helpers: split oversized files so they fit Groq Whisper's size limit.

Groq's transcription endpoint caps upload size (~25 MB). Long meetings exceed it,
so we split into time-based chunks and transcribe each. WAV is split with the
stdlib `wave` module (no dependency). Other formats use pydub if installed.
"""

from __future__ import annotations

import contextlib
import wave
from pathlib import Path
from typing import List

# stay safely under Groq's ~25 MB cap
DEFAULT_MAX_BYTES = 24 * 1024 * 1024


def file_size(path: str) -> int:
    return Path(path).stat().st_size


def split_wav(path: str, out_dir: str, max_bytes: int = DEFAULT_MAX_BYTES) -> List[str]:
    """Split a .wav into consecutive chunks each <= max_bytes. Returns chunk paths.

    Returns [path] unchanged if it already fits.
    """
    if file_size(path) <= max_bytes:
        return [path]

    with contextlib.closing(wave.open(path, "rb")) as w:
        nchannels = w.getnchannels()
        sampwidth = w.getsampwidth()
        framerate = w.getframerate()
        nframes = w.getnframes()
        block = nchannels * sampwidth  # bytes per frame
        # leave headroom for the 44-byte header
        frames_per_chunk = max(1, (max_bytes - 1024) // block)

        chunks: List[str] = []
        idx = 0
        remaining = nframes
        while remaining > 0:
            n = min(frames_per_chunk, remaining)
            frames = w.readframes(n)
            out = str(Path(out_dir) / f"chunk_{idx:03d}.wav")
            with contextlib.closing(wave.open(out, "wb")) as cw:
                cw.setnchannels(nchannels)
                cw.setsampwidth(sampwidth)
                cw.setframerate(framerate)
                cw.writeframes(frames)
            chunks.append(out)
            remaining -= n
            idx += 1
    return chunks


def split_via_pydub(path: str, out_dir: str, max_bytes: int = DEFAULT_MAX_BYTES) -> List[str]:
    """Split any pydub-readable audio (needs pydub + ffmpeg). Exports wav chunks."""
    from pydub import AudioSegment  # optional; needs ffmpeg on PATH

    audio = AudioSegment.from_file(path)
    total_ms = len(audio)
    size = file_size(path)
    if size <= max_bytes:
        return [path]
    parts = (size // max_bytes) + 1
    span = total_ms // parts + 1
    chunks: List[str] = []
    for i, start in enumerate(range(0, total_ms, span)):
        seg = audio[start:start + span]
        out = str(Path(out_dir) / f"chunk_{i:03d}.wav")
        seg.export(out, format="wav")
        chunks.append(out)
    return chunks
