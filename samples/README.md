# Samples

Try `meetmind` without recording a meeting first.

## Example input — ingest a transcript

`m1-kickoff.txt` and `m2-review.txt` are two short meeting transcripts where the
second reverses a decision from the first. Ingest them in order to watch the graph
supersede the old decision (needs a Groq key, or use the bundled shared one):

```bash
meetmind ingest samples/m1-kickoff.txt --meeting-id m1
meetmind ingest samples/m2-review.txt  --meeting-id m2
meetmind show        # current active nodes
meetmind delta       # what the second meeting changed
```

## Example output — explore a finished graph

`example-graphs/` holds three real per-project graphs produced by a single
multi-project meeting (routing split it into separate projects). No API key needed
to explore them:

```bash
meetmind show    --graph samples/example-graphs/coding-agent.json
meetmind export  --graph samples/example-graphs/voice-agent.json --format md
meetmind viz     --graph samples/example-graphs/marketing-automation.json --out mkt.png
```

Each file is a temporal graph: nodes carry a `status` (`active` / `superseded`) and
`supersedes` links, so it shows current truth *and* how it got there.
