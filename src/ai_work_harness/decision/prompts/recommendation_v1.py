"""Recommendation prompt v1."""

RECOMMENDATION_V1 = """\
Draft a qualitative recommendation from the supplied verified comparison. Use get_candidates,
get_criteria, and get_evidence to inspect the frozen records before submitting. Select only a
candidate listed as must-eligible; otherwise abstain. Cite only evidence IDs that exist in the
context, and explicitly state risks and uncertainties. Do not confirm inputs, review
evaluations, make a final human decision, or approve anything. End with exactly one closed
payload through submit_recommendation, and do not mix that submission with lookup calls.
"""
