# ADR-0004: Explicit v2 namespace and one-way v1 migration

- Status: accepted
- Context: reinterpreting the existing flat CLI or its boolean confirmation/free-text approval would
  break compatibility and overstate legacy evidence.
- Decision: preserve v1 commands and schemas; require `decision` for v2. Migration validates and copies
  input bytes into a separate session, converts at most a draft frame, emits a report, and never promotes
  v1 confirmation, route or approval into v2 gates.
- Consequences: users choose migration deliberately. Later v1 changes do not synchronize automatically,
  and v1 remains available for its original narrow routing workflow.
