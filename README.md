# ai-work-harness

Python-based human-gated AI build harness prototype

`ai-work-harness` records a small chain of local artifacts before a build decision is
accepted:

1. copy local inputs into managed storage and record their content hashes;
2. keep the user's statement separate from the AI interpretation;
3. require explicit user confirmation of the agreed frame;
4. select one deterministic route from a narrow registry; and
5. bind the input, frame, profile, route, decision, and reason into an approval record.

Old approval records remain available. `status` recomputes their validity against the
current files and reports why an approval is stale.

## Boundaries

This repository deliberately implements only the local artifact-recording, deterministic
routing, and approval-binding flow described below. It does not call a model, execute
generated work, verify a person's identity, make an approval immutable, or integrate with
remote services. Its hashes detect ordinary local drift; a person who can rewrite every
artifact can also recompute those hashes.

Only two routes are registered:

| Input source | Local modalities | Capability family | Route |
|---|---|---|---|
| `local_files` | any nonempty subset of `text`, `json`, `csv` | any nonempty subset of `transform`, `aggregate`, `rank` | `batch_pipeline` |
| `local_files` | any nonempty subset of `text`, `json`, `csv` | any nonempty subset of `validate`, `apply_rules` | `rule_decision` |

Network, live API, retrieval, multimodal, unregistered, and mixed paths fail closed with a
structured error and a nonzero exit code.

## Install

Python 3.11 and 3.12 are supported.

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ai-work-harness --version
```

## Synthetic walkthrough

The repository includes a small invented CSV under `examples/synthetic/`.

```bash
HARNESS=".venv/bin/ai-work-harness"
DEMO_ROOT="$(mktemp -d)"

"$HARNESS" --root "$DEMO_ROOT" init

"$HARNESS" --root "$DEMO_ROOT" capture \
  --id records.csv \
  --file examples/synthetic/records.csv

"$HARNESS" --root "$DEMO_ROOT" frame \
  --user-statement "Group the local records into a reviewable output." \
  --ai-interpretation "A deterministic batch transformation is sufficient." \
  --confirmed \
  --agreed-frame "Produce a local batch artifact without external access."

"$HARNESS" --root "$DEMO_ROOT" route \
  --input-source local_files \
  --modality csv \
  --capability transform \
  --capability aggregate \
  --objective "Create a deterministic grouped artifact."

"$HARNESS" --root "$DEMO_ROOT" approve \
  --decision "Use the batch pipeline route." \
  --reason "The confirmed frame and local input match the registered route."

"$HARNESS" --root "$DEMO_ROOT" status
"$HARNESS" --root "$DEMO_ROOT" verify
```

All command results are JSON. Expected workflow failures are also JSON, written to stderr,
and return exit code `2`.

## Local artifacts

`init` creates `.ai-work-harness/`, which is ignored by Git.

```text
.ai-work-harness/
├── input-manifest.json
├── inputs/
├── frame.json
├── task-profile.json
├── route.json
└── approvals/
```

Each input manifest entry contains only:

- a safe relative `logical_id`;
- its byte count; and
- its SHA-256 digest.

The manifest does not store source paths, timestamps, content previews, or environment
metadata. Managed input paths are derived from validated logical IDs.

Approval records bind the current input manifest, frame, task profile, route, decision, and
reason. A later `capture`, or a changed frame/profile/route, leaves the older record in place
but makes it stale.

## Schema and deterministic output

Versioned JSON Schemas are packaged under `src/ai_work_harness/schemas/v1/`. Runtime writes
are validated with JSON Schema Draft 2020-12, and every object rejects undeclared fields.
Persisted JSON uses UTF-8, sorted keys, compact separators, and a final newline. The route
artifact contains no clock-derived value, so the same canonical inputs produce identical
bytes.

## Development checks

```bash
ruff check .
ruff format --check .
pytest
python -m pip wheel . --no-deps --wheel-dir dist
```

The CI matrix runs lint and tests on Python 3.11 and 3.12 from a clean checkout.

## License

MIT
