# ADR-0005: Guided human interface over the stable transactional interface

- Status: accepted
- Target release: v0.5.0

## Context

The v2 raw CLI exposes the storage protocol faithfully: every mutation names a session and an exact
parent snapshot, confirmations bind a full artifact digest, and approval commit repeats the challenge
binding. Those fields are useful to scripts, CI, MCP adapters, incident analysis, and reproducible tests.
They are poor primary controls for a person, however. Copying hashes does not demonstrate that the
operator understood the candidates, criteria, evidence, risks, or the difference between an AI
recommendation and the human decision.

Removing expected-parent CAS or joining separate human events would weaken the product's guarantees.
Making the raw CLI implicit would also break existing automation and make concurrent writes harder to
diagnose.

## Decision

Keep the existing raw v2 commands, JSON envelopes, explicit parents, and approval binding arguments
stable. Add a separate TTY-only human interface:

```text
ai-work-harness decision guide <session-id> [--lang ko|en]
```

The guide reads and verifies one pinned snapshot, shows semantic summaries, and passes cryptographic
values internally. It advances its cursor only from a successful service response. A concurrent write
still fails with `WRITE_CONFLICT`; the guide never retries a mutation against a parent the operator did
not review.

Add `decision next --session-id <id>` as the read-only machine interface to the same pure planner. Its
`operator-plan.v1` result is sanitized: nonce, full challenge binding, raw source, and excerpts remain
only in the trusted in-process read model.

Human gates remain separate artifacts and snapshots. Guided frame, candidate, and criteria
confirmations use the additive `guided_semantic_review` method; existing raw confirmations retain
`digest_challenge`. Guided OpenAI consent uses the additive `guided_exact_phrase` method and requires
`SEND OPENAI <manifest-fingerprint>` before the first external request. A failed provider run may resume
from the still-active consent only when its stored full manifest is unchanged; a different manifest
requires a new consent. Final approval likewise requires a disposition-, target-, and bundle-bearing
phrase. MCP and providers receive no confirmation, review, final-decision, challenge, or approval
authority.

## Alternatives considered

- Make `--session-id` and `--expected-parent` optional everywhere: rejected because it changes the raw
  automation contract and makes stale writes easier to apply accidentally.
- Persist a global active session: rejected because hidden cross-project state can target the wrong
  decision.
- Join import/confirm or challenge/commit into one mutation: rejected because it erases the independent
  human event and snapshot boundary.
- Build a full-screen TUI or web service: deferred; a standard-library line interface is sufficient for
  the local single-operator product and is easier to test across supported platforms.

## Consequences

Human operators use one resumable command and no longer copy cryptographic values. Automation keeps a
precise, backward-compatible protocol. The project owns two presentations but only one transition
authority: `DecisionService`. Planner and service must share pure review and risk policy helpers so the
guide cannot promise an action the core later interprets differently.

## Implementation note

Implemented in v0.5.0 across `feat/guided-decision-ux`, `feat/guided-decision-workflow`, and
`feat/guided-decision-integrations`. The additive confirmation methods keep the artifact envelope at
schema version `2.0`, so existing v0.4 sessions verify without migration. Raw commands retain their
original arguments and `digest_challenge` behavior.
