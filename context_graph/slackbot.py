"""Slack bot: drop a meeting recording in a channel -> context graph + rendered image.

Flow:
  1. A user uploads an audio file (.wav/.mp3/.m4a/.mp4) to a channel the bot is in.
  2. The bot downloads it, transcribes via Groq Whisper, runs the ingest pipeline
     (extract -> merge -> graph.json -> git commit).
  3. It posts the changelog and uploads a rendered PNG of the current graph.

Mentions:
  @bot graph   -> post the current graph image
  @bot show    -> list active nodes as text
  @bot delta   -> post the git diff from the last meeting

Run (Socket Mode — no public URL needed):
  export SLACK_BOT_TOKEN=xoxb-...     # bot token, scopes below
  export SLACK_APP_TOKEN=xapp-...     # app-level token with connections:write
  export GROQ_API_KEY=...
  export CG_GRAPH_PATH=graph.json     # optional, default graph.json
  python -m context_graph.slackbot

Required bot scopes: app_mentions:read, channels:history, channels:read,
  files:read, files:write, chat:write, groups:history + groups:read (private).
  (channels:read/groups:read let the bot look up the channel name for per-channel
  projects — see CG_PROJECT_PER_CHANNEL below.)
Enable Socket Mode + subscribe to events: message.channels, app_mention.

Per-channel projects (default ON): each channel becomes its own project graph at
context-graphs/<channel-name>.json. So #payments and #auth keep separate graphs.
Set CG_PROJECT_PER_CHANNEL=0 to use a single shared graph (CG_GRAPH_PATH) instead.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path

from . import docread, pipeline, store, transcribe, viz

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".mp4", ".ogg", ".webm", ".flac"}


# --- config ------------------------------------------------------------------


class Config:
    def __init__(self) -> None:
        self.include_superseded = os.environ.get("CG_SHOW_SUPERSEDED", "1") != "0"
        # per-channel projects (default on): each channel -> its own project graph
        self.per_channel = os.environ.get("CG_PROJECT_PER_CHANNEL", "1") != "0"
        self.graph_dir = os.environ.get("CG_GRAPH_DIR", "context-graphs")
        # single-graph fallback when per-channel is off
        self.single_graph = os.environ.get("CG_GRAPH_PATH", "graph.json")
        # multi-project routing: split a meeting across per-project graphs by content
        self.route_projects = os.environ.get("CG_ROUTE_PROJECTS", "0") != "0"

    def graph_path(self, project_slug: str | None) -> str:
        if self.per_channel and project_slug:
            return str(Path(self.graph_dir) / f"{project_slug}.json")
        return self.single_graph

    def repo_dir(self, graph_path: str) -> str:
        return str(Path(graph_path).resolve().parent)


# --- pure-ish helpers (unit-testable without Slack) --------------------------


def is_audio(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in AUDIO_EXTS


def meeting_id_from_filename(filename: str) -> str:
    import re

    stem = Path(filename or "meeting").stem
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return slug or "meeting"


_MENTION_RE = re.compile(r"<@[^>]+>")
_INGEST_PREFIX = re.compile(r"^\s*(ingest|note|notes|mom|minutes|add)\b[:\-\s]*", re.IGNORECASE)


def _strip_mentions(text: str) -> str:
    return _MENTION_RE.sub(" ", text or "").strip()


def parse_mention(text: str, bot_user_id: str | None = None) -> tuple[str, str]:
    """Return (command, payload). command ∈ {graph, show, delta, ingest, help}.

    - "@bot note <text>" / "ingest/mom/minutes/add <text>" -> ('ingest', <text>)
    - a bare command word -> that command, empty payload
    - any other substantial pasted text -> ('ingest', <text>)  (tag-and-paste)
    """
    body = _strip_mentions(text)
    low = body.lower()

    m = _INGEST_PREFIX.match(body)
    if m:
        return "ingest", body[m.end():].strip()

    words = low.split()
    # commands that take an argument (a node id, or 'all')
    if words and words[0] in ("confirm", "approve"):
        return "confirm", " ".join(body.split()[1:]).strip()
    if words and words[0] in ("reject", "discard"):
        return "reject", " ".join(body.split()[1:]).strip()

    # short, command-like messages
    if len(words) <= 4:
        if any(w in low for w in ("delta", "diff", "change")):
            return "delta", ""
        if any(w in low for w in ("timeline", "history", "evolution")):
            return "timeline", ""
        if any(w in low for w in ("review", "proposed", "pending")):
            return "review", ""
        if any(w in low for w in ("show", "list", "nodes")):
            return "show", ""
        if any(w in low for w in ("graph", "image", "picture", "viz")):
            return "graph", ""
        if any(w in low for w in ("help", "hi", "hello", "?")):
            return "help", ""

    # substantial pasted text with no command -> treat as notes to ingest
    if len(words) >= 8:
        return "ingest", body
    return "help", ""


def parse_command(text: str, bot_user_id: str | None = None) -> str:
    """Back-compat: command only."""
    return parse_mention(text, bot_user_id)[0]


def render_summary(changelog: list[str]) -> str:
    if not changelog:
        return "No graph changes from this meeting."
    return "*Context graph updated:*\n" + "\n".join(f"• {line}" for line in changelog)


def active_nodes_text(graph) -> str:
    lines = []
    for n in graph.active_nodes():
        owner = f" _(@{n.owner})_" if n.owner else ""
        lines.append(f"*[{n.type.value}]* {n.title}{owner}\n    {n.content}")
    return "\n".join(lines) if lines else "_(graph is empty)_"


# --- Slack-facing operations -------------------------------------------------


def _download_slack_file(client, file_obj, dest_dir: str) -> str:
    """Download a Slack file to dest_dir, return the local path."""
    import requests

    info = client.files_info(file=file_obj["id"])["file"]
    url = info.get("url_private_download") or info["url_private"]
    token = client.token
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=120)
    resp.raise_for_status()
    local = Path(dest_dir) / (info.get("name") or f"{file_obj['id']}.wav")
    local.write_bytes(resp.content)
    return str(local)


_channel_cache: dict[str, tuple[str, str]] = {}


def channel_project(client, channel_id: str) -> tuple[str, str]:
    """Map a Slack channel to (project_slug, display_name). Cached per channel.

    The channel NAME becomes the project, so #payments and #auth build separate
    graphs (context-graphs/payments.json, context-graphs/auth.json).
    """
    if channel_id in _channel_cache:
        return _channel_cache[channel_id]
    name = channel_id
    try:
        info = client.conversations_info(channel=channel_id)["channel"]
        name = info.get("name") or channel_id
    except Exception:
        pass
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or channel_id
    _channel_cache[channel_id] = (slug, name)
    return slug, name


def _post_graph_image(client, channel: str, cfg: Config, title: str) -> None:
    slug, _ = channel_project(client, channel)
    graph = store.load_graph(cfg.graph_path(slug))
    if not graph.nodes:
        client.chat_postMessage(channel=channel, text="_Graph is empty — ingest a meeting first._")
        return
    with tempfile.TemporaryDirectory() as td:
        png = viz.render_png(graph, str(Path(td) / "graph.png"), cfg.include_superseded)
        client.files_upload_v2(channel=channel, file=png, title=title,
                               initial_comment=f"Context graph ({len(graph.active_nodes())} active nodes)")


def _post_review_prompt(client, channel: str, graph) -> None:
    """If the ingest produced low-confidence 'proposed' nodes, ask for approval."""
    from . import review

    proposed = review.proposed_nodes(graph)
    if not proposed:
        return
    lines = [f":eyes: *{len(proposed)} low-confidence item(s) need review* — "
             "reply `confirm <id>` / `reject <id>` (or `confirm --all`):"]
    for n in proposed:
        sup = f" _(would replace {', '.join(n.supersedes)})_" if n.supersedes else ""
        lines.append(f"• `{n.id}` [{n.type.value}] {n.title} — conf {n.confidence:.2f}{sup}")
    client.chat_postMessage(channel=channel, text="\n".join(lines))


def _finish_ingest(client, channel: str, cfg: Config, text: str, meeting_id: str) -> None:
    """Shared tail: ingest `text` and report. Routes across projects when enabled."""
    if cfg.route_projects:
        _finish_ingest_routed(client, channel, cfg, text, meeting_id)
        return
    slug, display = channel_project(client, channel)
    graph_path = cfg.graph_path(slug)
    graph, changelog = pipeline.ingest_transcript(
        text, meeting_id, graph_path, project=display if cfg.per_channel else None
    )
    client.chat_postMessage(channel=channel, text=render_summary(changelog))
    _post_graph_image(client, channel, cfg, title=f"Graph after {meeting_id}")
    _post_review_prompt(client, channel, graph)


def _finish_ingest_routed(client, channel: str, cfg: Config, text: str, meeting_id: str) -> None:
    """Multi-project: split the meeting across per-project graphs, report each."""
    results = pipeline.ingest_transcript_routed(text, meeting_id, cfg.graph_dir)
    touched = ", ".join(display for _, (_, _, display) in results.items())
    client.chat_postMessage(channel=channel,
                            text=f":card_index_dividers: This meeting touched *{len(results)}* project(s): {touched}")
    for slug, (graph, changelog, display) in results.items():
        client.chat_postMessage(channel=channel,
                                text=f"*{display}*\n" + render_summary(changelog))
        with tempfile.TemporaryDirectory() as td:
            if graph.nodes:
                png = viz.render_png(graph, str(Path(td) / f"{slug}.png"), cfg.include_superseded)
                client.files_upload_v2(channel=channel, file=png, title=f"{display} graph",
                                       initial_comment=f"{display}: {len(graph.active_nodes())} active nodes")
        _post_review_prompt(client, channel, graph)


def process_audio_file(client, channel: str, file_obj: dict, cfg: Config) -> None:
    """Download -> transcribe -> ingest -> post changelog + graph image."""
    name = file_obj.get("name", "recording")
    try:
        with tempfile.TemporaryDirectory() as td:
            local = _download_slack_file(client, file_obj, td)
            client.chat_postMessage(channel=channel,
                                    text=f":hourglass_flowing_sand: Transcribing *{name}* …")
            text = transcribe.from_audio(local)
        _finish_ingest(client, channel, cfg, text, meeting_id_from_filename(name))
    except Exception as err:  # never let a bad file kill the bot
        client.chat_postMessage(channel=channel, text=f":x: Failed on *{name}*: {err}")


def process_document_file(client, channel: str, file_obj: dict, cfg: Config) -> None:
    """Download a document (.txt/.md/.vtt/.pdf/.docx…) -> extract text -> ingest."""
    name = file_obj.get("name", "document")
    try:
        with tempfile.TemporaryDirectory() as td:
            local = _download_slack_file(client, file_obj, td)
            client.chat_postMessage(channel=channel, text=f":page_facing_up: Reading *{name}* …")
            text = docread.extract_text(local)
        _finish_ingest(client, channel, cfg, text, meeting_id_from_filename(name))
    except Exception as err:
        client.chat_postMessage(channel=channel, text=f":x: Failed on *{name}*: {err}")


def process_pasted_text(client, channel: str, text: str, meeting_id: str, cfg: Config) -> None:
    """Ingest text pasted into a mention (@bot note ...)."""
    try:
        client.chat_postMessage(channel=channel, text=":memo: Ingesting pasted notes …")
        _finish_ingest(client, channel, cfg, text, meeting_id)
    except Exception as err:
        client.chat_postMessage(channel=channel, text=f":x: Failed to ingest notes: {err}")


# --- app wiring --------------------------------------------------------------


def build_app(cfg: Config | None = None):
    from slack_bolt import App

    cfg = cfg or Config()
    app = App(token=os.environ["SLACK_BOT_TOKEN"])
    _seen_files: set[str] = set()  # dedup Slack event retries

    def _spawn(target, *args):
        threading.Thread(target=target, args=args, daemon=True).start()

    def _handle_files(client, channel, files) -> int:
        """Route each attached file to audio/document ingestion. Returns count handled."""
        handled = 0
        for f in files or []:
            name = f.get("name", "")
            if f.get("id") in _seen_files:
                continue
            if is_audio(name):
                _seen_files.add(f["id"])
                _spawn(process_audio_file, client, channel, f, cfg)
                handled += 1
            elif docread.is_document(name):
                _seen_files.add(f["id"])
                _spawn(process_document_file, client, channel, f, cfg)
                handled += 1
        return handled

    @app.event("message")
    def on_message(event, client, logger):
        if event.get("subtype") not in (None, "file_share"):
            return
        _handle_files(client, event["channel"], event.get("files"))

    @app.event("app_mention")
    def on_mention(event, client):
        channel = event["channel"]
        # a file attached to the mention (e.g. PDF + @bot) is ingested too
        if _handle_files(client, channel, event.get("files")):
            return
        cmd, payload = parse_mention(event.get("text", ""), event.get("bot_id"))
        slug, display = channel_project(client, channel)
        graph_path = cfg.graph_path(slug)
        if cmd == "ingest":
            if not payload.strip():
                client.chat_postMessage(channel=channel,
                                        text="Nothing to ingest — paste some notes after the mention.")
                return
            meeting_id = f"note-{event.get('ts', 'paste').replace('.', '-')}"
            _spawn(process_pasted_text, client, channel, payload, meeting_id, cfg)
        elif cmd == "graph":
            _post_graph_image(client, channel, cfg, title=f"Context graph — {display}")
        elif cmd == "show":
            graph = store.load_graph(graph_path)
            header = f"*Project: {display}*\n" if cfg.per_channel else ""
            client.chat_postMessage(channel=channel, text=header + active_nodes_text(graph))
        elif cmd == "delta":
            diff = store.git_diff_last(cfg.repo_dir(graph_path), Path(graph_path).name)
            client.chat_postMessage(
                channel=channel,
                text="```" + (diff[:3500] or "(no previous meeting to diff)") + "```",
            )
        elif cmd == "timeline":
            from . import timeline
            graph = store.load_graph(graph_path)
            client.chat_postMessage(channel=channel, text=timeline.to_markdown(graph)[:3500])
        elif cmd == "review":
            from . import review
            graph = store.load_graph(graph_path)
            proposed = review.proposed_nodes(graph)
            if not proposed:
                client.chat_postMessage(channel=channel, text="No proposed nodes awaiting review. ✅")
            else:
                lines = [f"*{len(proposed)} proposed node(s)* — reply `confirm <id>` / `reject <id>` (or `--all`):"]
                for n in proposed:
                    lines.append(f"• `{n.id}` [{n.type.value}] {n.title} _(conf {n.confidence:.2f})_")
                client.chat_postMessage(channel=channel, text="\n".join(lines))
        elif cmd in ("confirm", "reject"):
            from . import review
            graph = store.load_graph(graph_path)
            target = payload.strip()
            if target in ("", "--all", "all"):
                results = review.confirm_all(graph) if cmd == "confirm" else review.reject_all(graph)
            else:
                fn = review.confirm if cmd == "confirm" else review.reject
                results = [fn(graph, target)]
            store.save_graph(graph_path, graph)
            store.git_commit(cfg.repo_dir(graph_path), [graph_path], f"review: {cmd} {target or 'all'}")
            client.chat_postMessage(channel=channel, text="\n".join(results) or "(nothing to do)")
        else:
            client.chat_postMessage(
                channel=channel,
                text="I turn meetings into a context graph. You can:\n"
                     "• upload an *audio recording* (.wav/.mp3/…) — I transcribe + ingest it\n"
                     "• upload a *document* (.txt/.md/.vtt/.pdf/.docx) — I read + ingest it\n"
                     "• `@me note <paste your meeting notes>` — ingest pasted text\n"
                     "• `@me graph` / `show` / `delta` — view the current graph\n"
                     "• `@me timeline` — how decisions evolved across meetings\n"
                     "• `@me review` then `confirm <id>` / `reject <id>` — approve low-confidence items",
            )

    return app


def _require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit(
            "Missing required environment variable(s): " + ", ".join(missing) + "\n"
            "Set them before running, e.g. (PowerShell):\n"
            '  $env:SLACK_BOT_TOKEN="xoxb-..."\n'
            '  $env:SLACK_APP_TOKEN="xapp-..."\n'
            '  $env:GROQ_API_KEY="..."'
        )


def main() -> None:
    from .env import load_dotenv
    load_dotenv()
    _require_env("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "GROQ_API_KEY")
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    cfg = Config()
    app = build_app(cfg)
    print(f"context-graph Slack bot up. graph={cfg.graph_path}, "
          f"renderer={viz.available_renderer()}")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
