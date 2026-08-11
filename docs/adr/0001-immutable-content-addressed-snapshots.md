# ADR-0001: Immutable content-addressed snapshots

- Status: accepted
- Context: approval must remain bound to exact source, criteria, evaluation and decision bytes while
  concurrent local processes and crashes are possible.
- Decision: hash strict RFC 8785 JSON with SHA-256, store immutable objects/snapshots, and update a
  small current pointer through writer lock, full expected-parent CAS, fsync and atomic replace.
- Consequences: drift, stale writes and partial pointer updates fail closed. Orphans are harmless but
  need doctor reporting. This detects corruption; it is not a signed or tamper-proof ledger.
