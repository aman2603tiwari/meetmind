"""Get transcript text into the pipeline.

Three sources, in order of preference:
  1. from_file  — a Meetily-exported transcript (.txt / .md). Preferred.
  2. from_paste — pasted minutes / MoM text.
  3. from_audio — fallback: send raw audio to Groq Whisper (needs GROQ_API_KEY).
"""

from __future__ import annotations

import os
from pathlib import Path

GROQ_WHISPER_MODEL = "whisper-large-v3"


def from_file(path: str) -> str:
    """Read a transcript/document into text.

    Documents (.pdf .docx .vtt .srt .md .txt ...) go through docread; any other
    extension is read as plain UTF-8 text.
    """
    from . import docread

    if docread.is_document(path):
        text = docread.extract_text(path)
    else:
        text = Path(path).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"transcript file is empty: {path}")
    return text


def from_paste(text: str) -> str:
    if not text or not text.strip():
        raise ValueError("pasted text is empty")
    return text


def from_meetily(meeting_id: str | None = None, db_path: str | None = None):
    """Pull a transcript straight from Meetily's local DB (recording -> transcript).

    Returns (transcript_text, MeetilyMeeting). Latest meeting if id omitted.
    """
    from . import meetily  # lazy import so non-Meetily paths don't touch it

    return meetily.get_transcript(meeting_id=meeting_id, db_path=db_path)


def from_audio(path: str, api_key: str | None = None) -> str:
    """Transcribe an audio file to text.

    Provider (env CG_STT_PROVIDER = groq | assemblyai | auto):
      - assemblyai: speaker-diarized transcript (needs ASSEMBLYAI_API_KEY)
      - groq (default): Whisper; large files are auto-split into chunks
      - auto: assemblyai if ASSEMBLYAI_API_KEY is set, else groq
    """
    if not Path(path).exists():
        raise FileNotFoundError(path)

    provider = os.environ.get("CG_STT_PROVIDER", "auto").lower()
    if provider == "auto":
        provider = "assemblyai" if os.environ.get("ASSEMBLYAI_API_KEY") else "groq"

    if provider == "assemblyai":
        return _from_audio_assemblyai(path)
    return _from_audio_groq(path, api_key)


def _from_audio_groq(path: str, api_key: str | None = None) -> str:
    from ._apikey import resolve_groq_key
    api_key = resolve_groq_key(api_key)
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set — needed for Groq transcription")

    from groq import Groq  # imported lazily so file/paste paths need no groq install
    from . import audio

    client = Groq(api_key=api_key)

    def _one(p: str) -> str:
        with open(p, "rb") as fh:
            result = client.audio.transcriptions.create(
                file=(Path(p).name, fh.read()),
                model=GROQ_WHISPER_MODEL,
                response_format="text",
            )
        return result if isinstance(result, str) else getattr(result, "text", str(result))

    # chunk if the file is too big for one request
    if audio.file_size(path) <= audio.DEFAULT_MAX_BYTES:
        parts = [path]
    elif Path(path).suffix.lower() == ".wav":
        import tempfile
        tmp = tempfile.mkdtemp()
        parts = audio.split_wav(path, tmp)
    else:
        try:
            import tempfile
            parts = audio.split_via_pydub(path, tempfile.mkdtemp())
        except Exception as err:
            raise RuntimeError(
                f"audio file exceeds Groq's size limit and can't be split "
                f"({Path(path).suffix} needs pydub+ffmpeg, or convert to .wav): {err}"
            )

    text = "\n".join(t for t in (_one(p) for p in parts) if t and t.strip())
    if not text.strip():
        raise ValueError("transcription returned empty text")
    return text


def _from_audio_assemblyai(path: str) -> str:
    """Speaker-diarized transcription via AssemblyAI (handles large files itself)."""
    import time

    import requests

    key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not key:
        raise RuntimeError("ASSEMBLYAI_API_KEY not set — needed for diarized transcription")
    base = "https://api.assemblyai.com/v2"
    headers = {"authorization": key}

    with open(path, "rb") as fh:
        up = requests.post(f"{base}/upload", headers=headers, data=fh, timeout=300)
    up.raise_for_status()
    audio_url = up.json()["upload_url"]

    create = requests.post(
        f"{base}/transcript",
        headers=headers,
        json={"audio_url": audio_url, "speaker_labels": True},
        timeout=60,
    )
    create.raise_for_status()
    tid = create.json()["id"]

    while True:
        poll = requests.get(f"{base}/transcript/{tid}", headers=headers, timeout=60).json()
        status = poll.get("status")
        if status == "completed":
            break
        if status == "error":
            raise RuntimeError(f"AssemblyAI error: {poll.get('error')}")
        time.sleep(3)

    utterances = poll.get("utterances") or []
    if utterances:
        return "\n".join(f"Speaker {u['speaker']}: {u['text']}" for u in utterances)
    text = poll.get("text", "")
    if not text.strip():
        raise ValueError("transcription returned empty text")
    return text
