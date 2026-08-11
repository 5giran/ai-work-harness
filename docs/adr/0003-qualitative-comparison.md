# ADR-0003: Qualitative comparison without numeric scoring

- Status: accepted
- Context: arbitrary weights and aggregate scores can hide Must failures and imply a mathematically
  justified winner that the evidence does not support.
- Decision: criteria use must/high/medium/low without weights. Core computes an ordered result matrix,
  evidence coverage and Must eligibility, but no score or automatic winner. Recommendation and human
  final decision remain separate artifacts.
- Consequences: trade-offs stay visible and a human may choose differently from the AI. Consumers
  cannot sort by a fabricated total score.
