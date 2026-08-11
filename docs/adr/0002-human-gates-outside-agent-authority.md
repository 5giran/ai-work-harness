# ADR-0002: Human gates are outside provider and MCP authority

- Status: accepted
- Context: an AI-produced confirmation or approval would collapse the distinction between draft and
  accountable local decision.
- Decision: providers receive immutable DTOs and return draft payloads only. MCP exposes no confirm,
  review, final-decision, challenge or approval tool. Core constructs human event identity fields and
  records `identity_verified=false`.
- Consequences: automation stops at explicit operator gates. This prevents accidental agent approval
  but does not authenticate the person at the terminal.
