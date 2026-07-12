"""Minimal .env loader (no dependency).

Reads KEY=VALUE lines from a .env file and puts them in os.environ WITHOUT
overwriting variables already set in the real environment (so an explicitly
exported var always wins). Supports:
  - `#` comments and blank lines
  - optional `export ` prefix
  - single/double quoted values
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | None = None, override: bool = False) -> bool:
    """Load a .env file. Returns True if a file was found and read.

    If `path` is None, looks for `.env` in the current dir and then in this
    package's parent (the repo root).
    """
    candidates = []
    if path:
        candidates.append(Path(path))
    else:
        candidates.append(Path.cwd() / ".env")
        candidates.append(Path(__file__).resolve().parent.parent / ".env")

    envfile = next((p for p in candidates if p.is_file()), None)
    if envfile is None:
        return False

    for raw in envfile.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
    return True
