<div align="center">

# meetmind

**Turn meetings into a living context graph your coding agent can build from.**

[![PyPI version](https://img.shields.io/pypi/v/meetmind.svg)](https://pypi.org/project/meetmind/)
[![Python](https://img.shields.io/pypi/pyversions/meetmind.svg)](https://pypi.org/project/meetmind/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

Every meeting produces decisions, requirements, and constraints — then they're lost in a
transcript nobody re-reads. `meetmind` extracts them into a **temporal knowledge graph**
that stays current across meetings: when a later call reverses a decision, the old one is
marked *superseded* and linked to its replacement. A coding agent reads the graph as the
source of truth, and the git diff of each meeting tells it exactly what changed.

```
recording / paste / doc ─▶ extract (Groq) ─▶ merge into graph.json (git) ─▶ coding agent
        (Meetily, free)         └── decisions · requirements · constraints · supersessions ──┘
```

## Features

- **Temporal graph, not notes** — every node has a `status` (`active` / `superseded` / `deprecated`) and a `supersedes` link, so the graph always shows current truth *and* what changed.
- **Many inputs** — a Meetily recording, an audio file, a pasted summary, or a transcript file.
- **`record` and walk away** — arm a meeting, and meetmind auto-ingests the moment the call ends.
- **Multi-project routing** — one meeting that touches several projects fans out into separate per-project graphs, with cross-project dependency edges preserved.
- **Agent-ready export** — emit a clean, active-only spec (Markdown or JSON) grouped by feature.
- **Git-native history** — each meeting is a commit; `meetmind delta` is the change set to act on.
- **Slack bot** — drop a recording in a channel, get the graph back as an image.
- **Works out of the box** — a shared Groq key ships with the package; set your own for heavier use.

## Installation

```bash
pip install meetmind             # core CLI
pip install "meetmind[all]"      # + Slack bot, graph images, PDF/DOCX ingest, embeddings
```

> [!TIP]
> meetmind runs with **zero setup** thanks to a bundled shared Groq key. For anything beyond
> light use, get a free key at [console.groq.com](https://console.groq.com) and set
> `GROQ_API_KEY` to avoid the shared rate limit.

Optional extras: `[bot]` (Slack), `[viz]` (PNG graphs — also needs the `dot` binary),
`[docs]` (PDF/DOCX), `[embeddings]` (semantic entity resolution).

## Quick start

### Record a live meeting

Capture with [Meetily](https://github.com/Zackriya-Solutions/meetily) (free, local, private),
and let meetmind watch for the finished transcript:

```bash
meetmind record --link https://meet.google.com/xyz
```

meetmind arms, waits for you to start recording in Meetily, detects when the call ends
(the transcript stops growing), then ingests and commits automatically. Press `Ctrl+C` to
finalize early. Add `--route` to split a multi-project meeting across graphs.

### Ingest anything else

```bash
meetmind ingest --meetily                       # latest Meetily meeting
meetmind ingest --paste "we'll use GraphQL and Postgres" --meeting-id m1
meetmind ingest transcript.txt --meeting-id 2026-07-12-kickoff
meetmind ingest --audio call.mp3 --meeting-id m1        # Groq Whisper fallback
meetmind ingest --new                           # every Meetily meeting not yet in the graph
```

### See and use the graph

```bash
meetmind show                    # current active nodes
meetmind delta                   # git diff of the last meeting's changes
meetmind timeline                # meeting-by-meeting evolution
meetmind viz --out graph.png     # render the graph
meetmind export --format md      # clean active-only spec for a coding agent
meetmind validate                # integrity check (dangling edges, supersede cycles)
```

## How the graph works

When a later meeting revises a decision, meetmind decides **new vs. duplicate vs. change**
using, in order: explicit supersession from the extractor, optional embedding similarity,
then string/concept matching. A changed decision doesn't overwrite the old one — the old
node flips to `superseded` and a `SUPERSEDES` edge points from the new node to it.

```json
{
  "id": "decision-api-style",
  "type": "Decision",
  "title": "API uses GraphQL",
  "content": "Switch the public API from REST to GraphQL.",
  "status": "active",
  "source_meeting": "2026-07-19-review",
  "supersedes": ["decision-api-rest"],
  "confidence": 0.9
}
```

Node types: `Feature`, `Requirement`, `Decision`, `Constraint`, `Component`, `Interface`,
`OpenQuestion`, `ActionItem`. Edges: `IMPLEMENTS`, `DEPENDS_ON`, `BLOCKS`, `SUPERSEDES`,
`DECIDED_IN`, `RELATES_TO`.

### For the coding agent

Point your agent at `graph.json` (or `meetmind export`) and treat it as follows:

- **Current requirements** = every `active` node. Obey these.
- **`superseded` / `deprecated`** nodes are not current truth — use them to know what to *replace*.
- **`OpenQuestion`** nodes are undecided — flag them, don't assume an answer.
- **What changed this meeting** = `meetmind delta`. A node flipping to `superseded` plus a
  new `active` node linked by `SUPERSEDES` means "replace the old implementation with the new."

## Multi-project meetings

When one meeting spans several projects, `--route` labels each node with a project and fans
them into separate `context-graphs/<project>.json` files — each with its own history,
supersession, and export spec.

```bash
meetmind ingest big-meeting.txt --meeting-id m1 --route
meetmind export --project payments
```

Known projects are discovered from existing graph files so labels stay consistent; a new
project name auto-creates its graph. **Cross-project dependencies** are preserved as
namespaced references (e.g. Billing `DEPENDS_ON` `auth:decision-api-graphql`) and surfaced
in the dependent project's export.

## Slack bot

Let anyone drop a meeting recording into a channel and get the graph back as an image.

```bash
pip install "meetmind[bot]"
export SLACK_BOT_TOKEN=xoxb-...      # bot token
export SLACK_APP_TOKEN=xapp-...      # app-level token, scope connections:write
export GROQ_API_KEY=...
meetmind-bot
```

Mentions: `@bot graph` (post the image), `@bot show` (list active nodes),
`@bot delta` (git diff), `@bot note <text>` (ingest pasted text). Uploaded audio and
documents are ingested automatically. Set `CG_ROUTE_PROJECTS=1` to route each meeting by
project instead of by channel.

> [!NOTE]
> The bot uses Socket Mode — no public URL needed. Enable Socket Mode, subscribe to the
> `message.channels` and `app_mention` events, and invite the bot to the channel.

## Configuration

| Variable | Purpose |
|----------|---------|
| `GROQ_API_KEY` | Your Groq key (overrides the bundled shared one). |
| `CG_CONFIDENCE_THRESHOLD` | Nodes below this confidence become `proposed` for review (default `0`). |
| `CG_ROUTE_PROJECTS` | `1` = bot routes meetings by project instead of by channel. |
| `GEMINI_API_KEY` | Enables Gemini embeddings for semantic entity resolution. |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Slack bot credentials. |

Any variable can also live in a `.env` file in your working directory.

## How it captures meetings

meetmind reads [Meetily](https://github.com/Zackriya-Solutions/meetily)'s local SQLite
database read-only — Meetily records system + mic audio and transcribes it locally for free,
so meetings never leave your machine. A previously recorded call works too: import it into
Meetily, or point meetmind straight at the file with `--audio` (Groq Whisper transcribes it).

> [!IMPORTANT]
> `record` **watches** Meetily — you start the recording once in Meetily, and everything
> after (detection, ingest, commit) is automatic. Meetily exposes no API to start recording
> programmatically.
