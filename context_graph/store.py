"""Load/save the graph JSON and commit it to git (the temporal history)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .schema import Graph


def load_graph(path: str) -> Graph:
    p = Path(path)
    if not p.exists():
        return Graph.empty()
    data = json.loads(p.read_text(encoding="utf-8"))
    return Graph.model_validate(data)


def save_graph(path: str, graph: Graph) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # by_alias so edges serialize as {"from","to","type"}
    text = graph.model_dump_json(indent=2, by_alias=True)
    p.write_text(text + "\n", encoding="utf-8")


def save_transcript(repo_dir: str, meeting_id: str, transcript: str) -> str:
    out = Path(repo_dir) / "transcripts" / f"{meeting_id}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(transcript, encoding="utf-8")
    return str(out)


def _git(repo_dir: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )


def ensure_repo(repo_dir: str) -> None:
    if not (Path(repo_dir) / ".git").exists():
        _git(repo_dir, "init")


def git_commit(repo_dir: str, paths: list[str], message: str) -> bool:
    """Stage paths and commit. Returns True if a commit was made."""
    ensure_repo(repo_dir)
    for path in paths:
        _git(repo_dir, "add", path)
    status = _git(repo_dir, "status", "--porcelain")
    if not status.stdout.strip():
        return False  # nothing changed
    result = _git(repo_dir, "commit", "-m", message)
    return result.returncode == 0


def git_diff_last(repo_dir: str, path: str) -> str:
    """The diff of `path` between the last two commits — the meeting delta."""
    result = _git(repo_dir, "diff", "HEAD~1", "HEAD", "--", path)
    return result.stdout
