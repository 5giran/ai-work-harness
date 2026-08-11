# Guided synthetic demo transcript — English

This is a curated transcript of the important checkpoints observed on 2026-08-12 KST.
It is intentionally shorter than the complete terminal log.

```bash
.venv/bin/python scripts/run_synthetic_decision_demo.py \
  --test-operator \
  --lang en
```

The run used only the synthetic files in `examples/decision-triage/` and made no external
provider call. `--test-operator` supplies deterministic CI input. It is not a person and is
not evidence of human identity, manual review, or authenticated approval.

Full snapshot and artifact digests, UUIDs, the nonce, and timestamps vary between runs and
are omitted. The 12-character fingerprints shown for semantic comparison are represented as
`<fingerprint>`.

## 1. One guide invocation

```text
Synthetic data only. No real customer data is used.
Session: triage-demo
The injected test operator is deterministic CI input,
not human identity or human-review evidence.

Create decision session 'triage-demo'? yes
Created decision session 'triage-demo'.

AI Work Harness · Guided Decision
Stage: Source capture
Verified: yes
```

The same guide controller carried the pinned snapshot forward. No raw mutation command or
cryptographic value was copied and pasted during the workflow.

## 2. Three human semantic gates

```text
Validated frame summary
Problem: Select an operating model for small-team Korean customer-inquiry triage.
Artifact fingerprint: <fingerprint>
Choice: Confirm this frame

Validated candidates summary
Candidates: classical-ml, llm-assisted, rules
Artifact fingerprint: <fingerprint>
Choice: Confirm these candidates

Validated criteria summary
Must: auditability, privacy
High: classification-quality
Medium: latency, operating-cost
Artifact fingerprint: <fingerprint>
Choice: Confirm these criteria
```

## 3. Nine Must/High reviews

The guide displayed each required cell in stable order with its cited observation,
assessment, and confidence. The test operator entered `concur` and a non-empty reason for
all nine cells.

| Order | Candidate | Criterion | Priority | Fixture assessment | Review |
|---:|---|---|---|---|---|
| 1 | `classical-ml` | auditability | Must | meets | concur |
| 2 | `classical-ml` | classification-quality | High | meets | concur |
| 3 | `classical-ml` | privacy | Must | meets | concur |
| 4 | `llm-assisted` | auditability | Must | partial | concur |
| 5 | `llm-assisted` | classification-quality | High | meets | concur |
| 6 | `llm-assisted` | privacy | Must | fails | concur |
| 7 | `rules` | auditability | Must | meets | concur |
| 8 | `rules` | classification-quality | High | partial | concur |
| 9 | `rules` | privacy | Must | meets | concur |

```text
Record these completed reviews? yes
Stage: Deterministic comparison
Verified: yes
```

Only `classical-ml` and `rules` passed every Must criterion. The Must failure for
`llm-assisted` was not bypassed by scoring or an automatic winner.

## 4. Recommendation and human decision diverge

```text
AI recommendation: select classical-ml

Human final decision
Disposition: select
Candidate: rules
Decision reason: Rules are the simplest reviewed operating choice for this small team.

[Risk to acknowledge]
rules / classification-quality
rules / latency
rules / operating-cost

Recommendation relation: different
Record this final decision? yes
```

The stored final decision has the following semantics.

```text
Fixture recommendation: classical-ml
Human final selection: rules
Recommendation relation: different
Risk acknowledgements:
  - rules/classification-quality
  - rules/latency
  - rules/operating-cost
```

## 5. Exact approval and current export

The guide did not expose or ask the operator to re-enter the full bundle digest, challenge
ID, or nonce. The operator re-entered the exact phrase containing the displayed target and
12-character fingerprint.

```text
[Decision approval brief]
Disposition: select · candidate: Rules [rules]
Recommendation relation: different
AI recommendation: select classical-ml

Final approval
Decision bundle fingerprint: <fingerprint>
Type exactly: APPROVE rules <fingerprint>
> APPROVE rules <fingerprint>
Approval reason: Reviewed the complete synthetic decision bundle.

Result: approval=approved, final=select, ready=true
Choice: Export viewer JSON
Viewer export complete.
```

The export omitted `--snapshot`, so it pinned the verified current snapshot at that point.

```text
Exported snapshot:
  verified: true
  decision_complete: true
  ready: true
  recommendation candidate: classical-ml
  final candidate: rules
  recommendation relation: different
```

## 6. Criteria change in the same invocation

After the export, the controller returned to the terminal menu and changed the criteria in
the same guide invocation.

```text
Choice: Change
Artifact to change: criteria_set
Active artifacts made stale:
  comparison, criteria_confirmation, decision_bundle,
  evaluation_review_set, evaluation_set, evidence_set,
  final_decision, human_approval, recommendation
Continue with this change? yes

JSON payload path: examples/decision-triage/criteria.changed.json
Choice: Quit and resume later
```

The current pointer then referenced the new criteria draft. The old approved objects remained
in immutable CAS.

```text
Current snapshot:
  verified: true
  decision_complete: false
  ready: false
  stale_reasons:
    - criteria_set_changed
```

The exported bundle therefore preserves the pre-change approved decision, while the current
session safely resumes at confirmation of the changed criteria.
