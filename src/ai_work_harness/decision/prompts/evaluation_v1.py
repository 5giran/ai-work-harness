"""Evaluation prompt v1."""

EVALUATION_V1 = """\
Draft one evaluation cell for every candidate and criterion listed in the context index.
Use get_candidates, get_criteria, and get_evidence to inspect the frozen records before
submitting. Use only source_observation evidence as factual support. User assertions and agent
inferences may explain uncertainty but are not factual evidence. A non-insufficient assessment
needs at least one supporting source_observation evidence ID. If support is not available, use
insufficient_evidence and state the uncertainty. Do not score, rank, confirm, review, make a
final decision, or approve anything. End with exactly one closed payload through
submit_evaluations, and do not mix that submission with lookup calls.
"""
