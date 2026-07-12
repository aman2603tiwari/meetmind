"""Unit tests for the added features: idempotency, review, timeline, docread,
validate, export, mention parsing, audio splitting. No network / API keys needed.

Run: python -m tests.test_features
"""

from __future__ import annotations

import sys
import tempfile
import wave
from pathlib import Path

from context_graph import docread, export, review, timeline, validate
from context_graph.merge import merge
from context_graph.schema import CandidateGraph, Graph, Node


def _cand(nodes):
    return CandidateGraph(nodes=nodes)


def test_confidence_review():
    graph = Graph.empty()
    graph, log = merge(graph, _cand([
        Node(id="req-a", type="Requirement", title="A", content="high conf", confidence=0.9),
        Node(id="req-b", type="Requirement", title="B", content="low conf", confidence=0.2),
    ]), "m1", "t1", confidence_threshold=0.5)

    assert graph.node_by_id("req-a").status == "active"
    assert graph.node_by_id("req-b").status == "proposed"
    assert len(review.proposed_nodes(graph)) == 1
    assert graph.active_nodes() and all(n.id != "req-b" for n in graph.active_nodes())

    # confirm promotes it
    review.confirm(graph, "req-b")
    assert graph.node_by_id("req-b").status == "active"
    print("confidence + review: OK")


def test_deferred_supersession_on_confirm():
    graph = Graph.empty()
    graph, _ = merge(graph, _cand([
        Node(id="decision-db-mysql", type="Decision", title="MySQL", content="use mysql"),
    ]), "m1", "t1")
    # low-confidence change: proposed, old stays active until confirmed
    graph, _ = merge(graph, _cand([
        Node(id="decision-db-postgres", type="Decision", title="Postgres",
             content="switch to postgres", confidence=0.2, supersedes=["decision-db-mysql"]),
    ]), "m2", "t2", confidence_threshold=0.5)

    assert graph.node_by_id("decision-db-mysql").status == "active", "supersession deferred"
    assert graph.node_by_id("decision-db-postgres").status == "proposed"
    review.confirm(graph, "decision-db-postgres")
    assert graph.node_by_id("decision-db-mysql").status == "superseded", "applied on confirm"
    assert graph.has_edge("decision-db-postgres", "decision-db-mysql", "SUPERSEDES")
    print("deferred supersession on confirm: OK")


def test_reject_removes():
    graph = Graph.empty()
    graph, _ = merge(graph, _cand([
        Node(id="req-x", type="Requirement", title="X", content="c", confidence=0.1),
    ]), "m1", "t1", confidence_threshold=0.5)
    review.reject(graph, "req-x")
    assert graph.node_by_id("req-x") is None
    print("reject removes: OK")


def test_timeline_and_export():
    graph = Graph.empty()
    graph.meta.project = "Proj"
    graph, _ = merge(graph, _cand([
        Node(id="feature-x", type="Feature", title="X feature", content="x"),
        Node(id="decision-api-rest", type="Decision", title="REST", content="rest"),
    ]), "m1", "t1")
    graph, _ = merge(graph, _cand([
        Node(id="decision-api-graphql", type="Decision", title="GraphQL",
             content="graphql", supersedes=["decision-api-rest"]),
    ]), "m2", "t2")

    tl = timeline.to_markdown(graph)
    assert "m1" in tl and "m2" in tl and "replaced" in tl.lower()
    assert "superseded" in tl.lower()

    spec = export.to_markdown(graph)
    assert "GraphQL" in spec and "REST" not in spec  # only active in export
    print("timeline + export: OK")


def test_validate_detects():
    graph = Graph.empty()
    graph.nodes.append(Node(id="a", type="Requirement", title="A", content="c", supersedes=["ghost"]))
    graph.add_edge("a", "missing", "RELATES_TO")
    issues = validate.validate(graph)
    assert any(i.severity == "error" for i in issues)
    print("validate detects: OK")


def test_docread_captions():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "c.vtt"
        p.write_text("WEBVTT\n\n1\n00:00:01.000 --> 00:00:03.000\nUse Redis.\n", encoding="utf-8")
        text = docread.extract_text(str(p))
    assert text == "Use Redis." and "-->" not in text
    print("docread captions: OK")


def test_mention_parsing():
    from context_graph.slackbot import parse_mention
    assert parse_mention("<@U1> graph", "U1")[0] == "graph"
    assert parse_mention("<@U1> note use Redis for caching", "U1") == ("ingest", "use Redis for caching")
    cmd, pay = parse_mention("<@U1> we decided the api must be graphql and auth via jwt tokens", "U1")
    assert cmd == "ingest" and "graphql" in pay
    print("mention parsing: OK")


def test_wav_split():
    from context_graph import audio
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "big.wav"
        # ~1.5s of silence, 44.1kHz 16-bit mono
        with wave.open(str(src), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
            w.writeframes(b"\x00\x00" * 66000)
        # force splitting with a tiny max_bytes
        parts = audio.split_wav(str(src), td, max_bytes=40000)
    assert len(parts) >= 2, f"expected multiple chunks, got {len(parts)}"
    print("wav split: OK")


def test_project_routing():
    """One meeting's candidate nodes split into separate per-project graphs."""
    from context_graph import pipeline, projects, store
    import context_graph.extract as ex

    def fake(transcript, meeting_id, api_key=None, existing_nodes=None, known_projects=None):
        return CandidateGraph(nodes=[
            Node(id="decision-invoice-pdf", type="Decision", title="Invoices PDF",
                 content="pdf", project="Billing"),
            Node(id="decision-auth-oauth", type="Decision", title="OAuth",
                 content="oauth", project="Auth"),
        ])
    ex.extract = pipeline.extract_mod.extract = fake

    with tempfile.TemporaryDirectory() as td:
        gd = str(Path(td) / "context-graphs")
        res = pipeline.ingest_transcript_routed("mixed", "m1", gd, commit=True)
        assert set(res.keys()) == {"billing", "auth"}, res.keys()
        billing = store.load_graph(str(Path(gd) / "billing.json"))
        auth = store.load_graph(str(Path(gd) / "auth.json"))
        assert [n.title for n in billing.active_nodes()] == ["Invoices PDF"]
        assert [n.title for n in auth.active_nodes()] == ["OAuth"]
        assert billing.meta.project == "Billing" and auth.meta.project == "Auth"
    print("project routing: OK")


def test_cross_project_edges():
    """An edge between nodes in different projects becomes a namespaced reference."""
    from context_graph import export, pipeline, store, validate
    import context_graph.extract as ex
    from context_graph.schema import Edge

    def fake(transcript, meeting_id, api_key=None, existing_nodes=None, known_projects=None):
        return CandidateGraph(
            nodes=[
                Node(id="req-bill-charge", type="Requirement", title="Charge via auth",
                     content="calls auth", project="Billing"),
                Node(id="decision-auth-api", type="Decision", title="Auth API",
                     content="graphql", project="Auth"),
            ],
            edges=[Edge(**{"from": "req-bill-charge", "to": "decision-auth-api", "type": "DEPENDS_ON"})],
        )
    ex.extract = pipeline.extract_mod.extract = fake

    with tempfile.TemporaryDirectory() as td:
        gd = str(Path(td) / "context-graphs")
        pipeline.ingest_transcript_routed("mixed", "m1", gd, commit=True)
        billing = store.load_graph(str(Path(gd) / "billing.json"))
        cross = [(e.src, e.dst) for e in billing.edges if ":" in e.dst]
        assert cross == [("req-bill-charge", "auth:decision-auth-api")], cross
        # not flagged as dangling
        assert not [i for i in validate.validate(billing) if i.severity == "error"]
        # surfaced in the export spec
        assert "auth:decision-auth-api" in export.to_markdown(billing)
    print("cross-project edges: OK")


def test_record_watch():
    """watch_for_recording detects a new meeting and finalises when it stabilises."""
    import sqlite3
    from context_graph import meetily, record

    with tempfile.TemporaryDirectory() as td:
        dbp = str(Path(td) / "meeting_minutes.sqlite")
        conn = sqlite3.connect(dbp)
        conn.execute("CREATE TABLE meetings (id TEXT, title TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE transcripts (meeting_id TEXT, transcript TEXT, "
                     "timestamp TEXT, speaker TEXT, audio_start_time REAL)")
        conn.execute("INSERT INTO meetings VALUES ('old', 'Old', '2026-07-01T00:00:00')")
        conn.commit()

        def add_meeting(mid):
            conn.execute("INSERT INTO meetings VALUES (?, ?, ?)",
                         (mid, "Live call", "2026-07-13T10:00:00"))
            conn.commit()

        def add_seg(mid, text):
            conn.execute("INSERT INTO transcripts VALUES (?, ?, ?, ?, ?)",
                         (mid, text, "2026-07-13T10:00:00", "mic", 0.0))
            conn.commit()

        baseline = meetily.meeting_ids(dbp)
        assert baseline == {"old"}

        clock = [0.0]
        actions = iter([
            lambda: None,                       # phase A: nothing new yet
            lambda: add_meeting("new"),         # new meeting appears
            lambda: add_seg("new", "Hello world"),   # transcript grows
            lambda: add_seg("new", "Second line"),   # grows again
            lambda: None,                       # no change
            lambda: None,                       # no change -> idle reached
        ])

        def time_fn():
            return clock[0]

        def sleep_fn(dt):
            clock[0] += dt
            try:
                next(actions)()
            except StopIteration:
                pass

        rec = record.watch_for_recording(
            db_path=dbp, poll=5.0, idle=10.0, baseline=baseline,
            time_fn=time_fn, sleep_fn=sleep_fn,
        )
        assert rec is not None and rec.meeting_id == "new", rec
        assert meetily.transcript_size("new", dbp) == len("Hello world") + len("Second line")
        conn.close()
    print("record watch: OK")


def test_record_stop_before_meeting():
    """Ctrl+C before any recording returns None (nothing to ingest)."""
    import sqlite3
    from context_graph import record

    with tempfile.TemporaryDirectory() as td:
        dbp = str(Path(td) / "meeting_minutes.sqlite")
        conn = sqlite3.connect(dbp)
        conn.execute("CREATE TABLE meetings (id TEXT, title TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE transcripts (meeting_id TEXT, transcript TEXT, "
                     "timestamp TEXT, speaker TEXT, audio_start_time REAL)")
        conn.commit()
        conn.close()

        rec = record.watch_for_recording(
            db_path=dbp, poll=1.0, idle=1.0, baseline=set(),
            stop_flag=lambda: True, sleep_fn=lambda _dt: None,
        )
        assert rec is None
    print("record stop before meeting: OK")


def main():
    for fn in [
        test_confidence_review, test_deferred_supersession_on_confirm, test_reject_removes,
        test_timeline_and_export, test_validate_detects, test_docread_captions,
        test_mention_parsing, test_wav_split, test_project_routing, test_cross_project_edges,
        test_record_watch, test_record_stop_before_meeting,
    ]:
        fn()
    print("\nALL FEATURE TESTS PASSED ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
