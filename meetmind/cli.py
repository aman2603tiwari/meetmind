"""CLI: transcript -> extract -> merge -> graph.json -> git commit.

Usage:
    python -m meetmind ingest TRANSCRIPT.txt --meeting-id 2026-07-12-standup
    python -m meetmind ingest --paste "we'll use REST..." --meeting-id m1
    python -m meetmind ingest --audio meeting.mp3 --meeting-id m1
    python -m meetmind ingest --meetily                 # latest Meetily meeting
    python -m meetmind ingest --new                     # all un-ingested Meetily meetings
    python -m meetmind ingest --since 2026-07-01        # Meetily meetings since a date
    python -m meetmind ingest ... --dry-run     # show changes, don't commit
    python -m meetmind show                      # print active nodes
    python -m meetmind delta                     # git diff of last meeting
    python -m meetmind meetily-list              # list Meetily meetings
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import pipeline, store, transcribe


def _slugify(text: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "meeting"


def _derive_meeting_id(meeting) -> str:
    """Readable id from a Meetily meeting: <created-date>-<title-slug>."""
    date = (meeting.created_at or "")[:10]
    return f"{date}-{_slugify(meeting.title)}" if date else _slugify(meeting.title)


def _ingest_one(graph, transcript, meeting_id, repo_dir, args, meetily_id=None):
    """Extract -> merge -> (write + commit) one meeting. Returns updated graph."""
    print(f"[extract] '{meeting_id}' ...", file=sys.stderr)
    graph, changelog = pipeline.ingest_transcript(
        transcript, meeting_id, args.graph,
        commit=not args.dry_run, meetily_id=meetily_id,
        project=getattr(args, "project", None),
    )
    print(f"=== changes for '{meeting_id}' ===")
    for line in changelog:
        print("  " + line)
    print(f"=== graph now: {len(graph.active_nodes())} active / {len(graph.nodes)} total ===\n")
    if args.dry_run:
        print("[dry-run] not written/committed.", file=sys.stderr)
    else:
        print("[commit] done.", file=sys.stderr)
    return graph


def _cmd_ingest_batch(args, repo_dir) -> int:
    """Batch-ingest multiple Meetily meetings (--since / --new / --all), oldest first."""
    from . import meetily

    graph = store.load_graph(args.graph)
    meetings = meetily.list_meetings_since(args.since, args.meetily_db)
    if args.new:
        meetings = [m for m in meetings if m.id not in graph.meta.meetily_ingested]
    if not meetings:
        print("No matching Meetily meetings to ingest.", file=sys.stderr)
        return 0

    print(f"Batch: {len(meetings)} meeting(s) to ingest (oldest first).", file=sys.stderr)
    ingested = 0
    for m in meetings:
        try:
            transcript, meeting = transcribe.from_meetily(meeting_id=m.id, db_path=args.meetily_db)
        except ValueError as err:  # e.g. no transcript segments yet
            print(f"  skip '{m.title}' ({m.id}): {err}", file=sys.stderr)
            continue
        meeting_id = _derive_meeting_id(meeting)
        graph = _ingest_one(graph, transcript, meeting_id, repo_dir, args, meetily_id=m.id)
        ingested += 1
    print(f"Batch done: {ingested} meeting(s) ingested.", file=sys.stderr)
    return 0


def cmd_ingest(args) -> int:
    repo_dir = str(Path(args.graph).resolve().parent)

    # batch mode: pull several Meetily meetings at once (these imply --meetily)
    if args.since is not None or args.new or args.all:
        return _cmd_ingest_batch(args, repo_dir)

    # single-meeting mode
    meetily_id = None
    if args.meetily or args.meetily_id is not None:
        transcript, meeting = transcribe.from_meetily(
            meeting_id=args.meetily_id, db_path=args.meetily_db
        )
        meetily_id = meeting.id
        suggested_id = _derive_meeting_id(meeting)
        print(f"Meetily meeting: '{meeting.title}' ({meeting.id})", file=sys.stderr)
    elif args.paste is not None:
        transcript, suggested_id = transcribe.from_paste(args.paste), None
    elif args.audio is not None:
        transcript, suggested_id = transcribe.from_audio(args.audio), None
    elif args.source:
        transcript, suggested_id = transcribe.from_file(args.source), None
    else:
        raise SystemExit(
            "provide a transcript file, --paste TEXT, --audio FILE, or --meetily / --meetily-id"
        )

    meeting_id = args.meeting_id or suggested_id
    if not meeting_id:
        raise SystemExit("--meeting-id is required (or use --meetily to derive one)")

    if args.route:
        return _cmd_ingest_routed(args, transcript, meeting_id)

    graph = store.load_graph(args.graph)
    _ingest_one(graph, transcript, meeting_id, repo_dir, args, meetily_id=meetily_id)
    return 0


def _cmd_ingest_routed(args, transcript, meeting_id) -> int:
    """Route ONE meeting into multiple per-project graphs (multi-project meeting)."""
    graph_dir = args.graph_dir or "context-graphs"
    print(f"[route] extracting + splitting '{meeting_id}' across projects ...", file=sys.stderr)
    results = pipeline.ingest_transcript_routed(
        transcript, meeting_id, graph_dir, commit=not args.dry_run
    )
    for slug, (graph, changelog, display) in results.items():
        print(f"\n=== {display} ({slug}.json) ===")
        for line in changelog:
            print("  " + line)
        print(f"    {len(graph.active_nodes())} active nodes")
    print(f"\nTouched {len(results)} project(s).", file=sys.stderr)
    if args.dry_run:
        print("[dry-run] nothing written/committed.", file=sys.stderr)
    return 0


def cmd_record(args) -> int:
    """Watch Meetily for a live recording, then auto-ingest when the call ends."""
    import signal
    from . import record

    if args.link:
        print(f"🎙️  Armed for: {args.link}", file=sys.stderr)
    print("    Start recording in Meetily now — I'll auto-ingest when the call ends.",
          file=sys.stderr)
    print("    (Press Ctrl+C to finalise immediately.)\n", file=sys.stderr)

    stop = {"v": False}

    def _on_sigint(*_):  # Ctrl+C -> graceful finalise, not a crash
        stop["v"] = True

    old = signal.signal(signal.SIGINT, _on_sigint)
    try:
        rec = record.watch_for_recording(
            db_path=args.meetily_db, poll=args.poll, idle=args.idle,
            status=lambda m: print("    " + m, file=sys.stderr),
            stop_flag=lambda: stop["v"],
        )
    finally:
        signal.signal(signal.SIGINT, old)

    if rec is None:
        print("No recording captured — nothing to ingest.", file=sys.stderr)
        return 1

    transcript, meeting = transcribe.from_meetily(
        meeting_id=rec.meeting_id, db_path=args.meetily_db
    )
    meeting_id = args.meeting_id or _derive_meeting_id(meeting)
    print(f"\nIngesting '{meeting.title}' as '{meeting_id}' ...", file=sys.stderr)

    if args.route:
        return _cmd_ingest_routed(args, transcript, meeting_id)
    repo_dir = str(Path(args.graph).resolve().parent)
    graph = store.load_graph(args.graph)
    _ingest_one(graph, transcript, meeting_id, repo_dir, args, meetily_id=rec.meeting_id)
    return 0


def cmd_show(args) -> int:
    graph = store.load_graph(args.graph)
    for node in graph.active_nodes():
        owner = f" [@{node.owner}]" if node.owner else ""
        print(f"[{node.type.value}] {node.title}{owner}\n    {node.content}\n    id={node.id}\n")
    return 0


def cmd_meetily_list(args) -> int:
    from . import meetily
    meetings = meetily.list_meetings(db_path=args.meetily_db, limit=args.limit)
    if not meetings:
        print("(no meetings found in Meetily database)")
        return 0
    for m in meetings:
        print(f"{m.created_at[:19]:<20} {m.id}\n    {m.title}")
    return 0


def cmd_delta(args) -> int:
    repo_dir = str(Path(args.graph).resolve().parent)
    diff = store.git_diff_last(repo_dir, str(Path(args.graph).name))
    print(diff or "(no previous commit to diff against)")
    return 0


def cmd_validate(args) -> int:
    from .validate import validate

    graph = store.load_graph(args.graph)
    issues = validate(graph)
    errors = [i for i in issues if i.severity == "error"]
    for i in issues:
        print(str(i))
    if not issues:
        print("✓ graph is valid — no issues.")
    print(f"\n{len(errors)} error(s), {len(issues) - len(errors)} warning(s).", file=sys.stderr)
    return 1 if errors else 0


def cmd_export(args) -> int:
    from . import export

    graph = store.load_graph(args.graph)
    text = export.to_json(graph) if args.format == "json" else export.to_markdown(graph)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote active-state spec ({args.format}) to {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_timeline(args) -> int:
    from . import timeline

    graph = store.load_graph(args.graph)
    text = timeline.to_mermaid(graph) if args.format == "mermaid" else timeline.to_markdown(graph)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote timeline ({args.format}) to {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_review(args) -> int:
    from . import review

    graph = store.load_graph(args.graph)
    proposed = review.proposed_nodes(graph)
    if not proposed:
        print("No proposed (low-confidence) nodes awaiting review.")
        return 0
    for n in proposed:
        sup = f" (would replace {', '.join(n.supersedes)})" if n.supersedes else ""
        print(f"[{n.type.value}] {n.title} — conf={n.confidence:.2f}{sup}\n    {n.content}\n    id={n.id}\n")
    print(f"{len(proposed)} proposed. Use: confirm <id>|--all  /  reject <id>|--all", file=sys.stderr)
    return 0


def _apply_review(args, action) -> int:
    from . import review

    graph = store.load_graph(args.graph)
    if args.all:
        results = review.confirm_all(graph) if action == "confirm" else review.reject_all(graph)
    elif args.id:
        fn = review.confirm if action == "confirm" else review.reject
        results = [fn(graph, args.id)]
    else:
        raise SystemExit(f"{action}: provide a node id or --all")
    for r in results:
        print(r)
    store.save_graph(args.graph, graph)
    repo_dir = str(Path(args.graph).resolve().parent)
    store.git_commit(repo_dir, [args.graph], f"review: {action} {args.id or 'all'}")
    return 0


def cmd_confirm(args) -> int:
    return _apply_review(args, "confirm")


def cmd_reject(args) -> int:
    return _apply_review(args, "reject")


def cmd_viz(args) -> int:
    from . import viz

    graph = store.load_graph(args.graph)
    if not graph.nodes:
        raise SystemExit("graph is empty — ingest a meeting first")

    include = not args.active_only
    if args.format in ("dot", "mermaid"):
        text = viz.to_dot(graph, include) if args.format == "dot" else viz.to_mermaid(graph, include)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"wrote {args.format} to {args.out}", file=sys.stderr)
        else:
            print(text)
        return 0

    # png
    out = args.out or "graph.png"
    print(f"rendering via {viz.available_renderer()} ...", file=sys.stderr)
    path = viz.render_png(graph, out, include)
    print(f"wrote {path}", file=sys.stderr)
    return 0


def _add_graph_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--graph", default=None,
                        help="explicit path to the graph json (default: graph.json, "
                             "or context-graphs/<project>.json with --project)")
    parser.add_argument("--project", default=None,
                        help="scope to a named project -> context-graphs/<project>.json")


def _resolve_graph_path(args) -> None:
    """Fill args.graph from --project when not given explicitly."""
    if getattr(args, "graph", None):
        return
    project = getattr(args, "project", None)
    if project:
        args.graph = str(Path("context-graphs") / f"{_slugify(project)}.json")
    else:
        args.graph = "graph.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="meetmind", description=__doc__)
    _add_graph_arg(p)  # also accepted before the subcommand
    sub = p.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="ingest a meeting into the graph")
    ing.add_argument("source", nargs="?", help="transcript file (.txt/.md)")
    ing.add_argument("--paste", help="pasted meeting notes text")
    ing.add_argument("--audio", help="audio file (fallback: Groq Whisper)")
    ing.add_argument("--meetily", action="store_true",
                     help="pull the latest transcript from Meetily's local DB")
    ing.add_argument("--meetily-id", help="pull a specific Meetily meeting by its id")
    ing.add_argument("--meetily-db", help="path to meeting_minutes.sqlite (auto-detected if omitted)")
    ing.add_argument("--since", metavar="ISO_DATE",
                     help="batch: ingest Meetily meetings created on/after this date/time "
                          "(e.g. 2026-07-01), oldest first")
    ing.add_argument("--new", action="store_true",
                     help="batch: ingest only Meetily meetings not already in the graph")
    ing.add_argument("--all", action="store_true",
                     help="batch: ingest every Meetily meeting")
    ing.add_argument("--meeting-id",
                     help="unique id, e.g. 2026-07-12-standup (auto-derived when using --meetily)")
    ing.add_argument("--route", action="store_true",
                     help="multi-project: split this meeting across per-project graphs")
    ing.add_argument("--graph-dir", default=None,
                     help="directory of per-project graphs for --route (default: context-graphs)")
    ing.add_argument("--dry-run", action="store_true", help="show changes without writing/committing")
    _add_graph_arg(ing)
    ing.set_defaults(func=cmd_ingest)

    rec = sub.add_parser("record",
                         help="watch Meetily for a live recording and auto-ingest when it ends")
    rec.add_argument("--link", help="meeting URL you're joining (shown as a label/reminder)")
    rec.add_argument("--meetily-db", help="path to meeting_minutes.sqlite (auto-detected if omitted)")
    rec.add_argument("--meeting-id", help="override the auto-derived meeting id")
    rec.add_argument("--route", action="store_true",
                     help="multi-project: split this meeting across per-project graphs")
    rec.add_argument("--graph-dir", default=None,
                     help="directory of per-project graphs for --route (default: context-graphs)")
    rec.add_argument("--poll", type=float, default=5.0,
                     help="seconds between DB checks (default: 5)")
    rec.add_argument("--idle", type=float, default=30.0,
                     help="seconds with no new speech = call ended (default: 30)")
    rec.add_argument("--dry-run", action="store_true", help="show changes without writing/committing")
    _add_graph_arg(rec)
    rec.set_defaults(func=cmd_record)

    ml = sub.add_parser("meetily-list", help="list recent meetings in Meetily's DB")
    ml.add_argument("--meetily-db", help="path to meeting_minutes.sqlite (auto-detected if omitted)")
    ml.add_argument("--limit", type=int, default=20)
    ml.set_defaults(func=cmd_meetily_list)

    sh = sub.add_parser("show", help="print current active nodes")
    _add_graph_arg(sh)
    sh.set_defaults(func=cmd_show)

    dl = sub.add_parser("delta", help="git diff of the last meeting's graph change")
    _add_graph_arg(dl)
    dl.set_defaults(func=cmd_delta)

    vz = sub.add_parser("viz", help="render the graph to a png / dot / mermaid")
    vz.add_argument("--out", help="output file (default: graph.png; stdout for dot/mermaid)")
    vz.add_argument("--format", choices=["png", "dot", "mermaid"], default="png")
    vz.add_argument("--active-only", action="store_true",
                    help="hide superseded/deprecated nodes")
    _add_graph_arg(vz)
    vz.set_defaults(func=cmd_viz)

    va = sub.add_parser("validate", help="check graph integrity")
    _add_graph_arg(va)
    va.set_defaults(func=cmd_validate)

    ex = sub.add_parser("export", help="export the ACTIVE-state spec for a coding agent")
    ex.add_argument("--format", choices=["md", "json"], default="md")
    ex.add_argument("--out", help="output file (default: stdout)")
    _add_graph_arg(ex)
    ex.set_defaults(func=cmd_export)

    tl = sub.add_parser("timeline", help="meeting-by-meeting evolution of the graph")
    tl.add_argument("--format", choices=["md", "mermaid"], default="md")
    tl.add_argument("--out", help="output file (default: stdout)")
    _add_graph_arg(tl)
    tl.set_defaults(func=cmd_timeline)

    rv = sub.add_parser("review", help="list proposed (low-confidence) nodes")
    _add_graph_arg(rv)
    rv.set_defaults(func=cmd_review)

    cf = sub.add_parser("confirm", help="confirm a proposed node (or --all)")
    cf.add_argument("id", nargs="?", help="node id to confirm")
    cf.add_argument("--all", action="store_true")
    _add_graph_arg(cf)
    cf.set_defaults(func=cmd_confirm)

    rj = sub.add_parser("reject", help="reject a proposed node (or --all)")
    rj.add_argument("id", nargs="?", help="node id to reject")
    rj.add_argument("--all", action="store_true")
    _add_graph_arg(rj)
    rj.set_defaults(func=cmd_reject)
    return p


def main(argv: list[str] | None = None) -> int:
    from .env import load_dotenv
    load_dotenv()  # so GROQ_API_KEY etc. can live in a .env file
    args = build_parser().parse_args(argv)
    _resolve_graph_path(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
