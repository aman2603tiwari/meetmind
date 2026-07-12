"""Read text out of an uploaded document so it can be ingested like a transcript.

Native (no dependency): .txt .md .markdown .vtt .srt .text .log
Optional (used if the library is installed):
    .pdf  -> pypdf
    .docx -> python-docx
"""

from __future__ import annotations

import re
from pathlib import Path

TEXT_EXTS = {".txt", ".md", ".markdown", ".vtt", ".srt", ".text", ".log"}
DOC_EXTS = TEXT_EXTS | {".pdf", ".docx"}


def is_document(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in DOC_EXTS


def _clean_captions(text: str) -> str:
    """Strip WebVTT/SRT timestamps and cue numbers, keep spoken text."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s == "WEBVTT" or s.isdigit():
            continue
        if "-->" in s:  # timestamp line
            continue
        out.append(s)
    return "\n".join(out)


def extract_text(path: str) -> str:
    ext = Path(path).suffix.lower()

    if ext in TEXT_EXTS:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        if ext in (".vtt", ".srt"):
            text = _clean_captions(text)
    elif ext == ".pdf":
        text = _read_pdf(path)
    elif ext == ".docx":
        text = _read_docx(path)
    else:
        raise ValueError(f"unsupported document type: {ext}")

    text = text.strip()
    if not text:
        raise ValueError(f"no text extracted from {Path(path).name}")
    return text


def _read_pdf(path: str) -> str:
    try:
        from pypdf import PdfReader
    except Exception as err:
        raise RuntimeError("PDF support needs 'pypdf' (pip install pypdf)") from err
    reader = PdfReader(path)
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _read_docx(path: str) -> str:
    try:
        import docx  # python-docx
    except Exception as err:
        raise RuntimeError("DOCX support needs 'python-docx' (pip install python-docx)") from err
    document = docx.Document(path)
    return "\n".join(p.text for p in document.paragraphs)
