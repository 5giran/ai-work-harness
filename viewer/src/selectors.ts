import type {
  CandidateView,
  CriterionView,
  DecisionViewBundle,
  EvaluationView,
  EvidenceView,
} from "./types";

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function items(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(record) : [];
}

function text(value: unknown, fallback = "—"): string {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function texts(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && item.length > 0)
    : [];
}

export function payload(
  bundle: DecisionViewBundle,
  artifact: string,
): Record<string, unknown> {
  return bundle.artifacts[artifact]?.payload ?? {};
}

export function frameView(bundle: DecisionViewBundle): {
  businessUser: string;
  blockedDecision: string;
  problemStatement: string;
} {
  const frame = payload(bundle, "decision_frame");
  return {
    businessUser: text(frame.business_user),
    blockedDecision: text(frame.blocked_decision),
    problemStatement: text(frame.problem_statement),
  };
}

export function candidatesView(bundle: DecisionViewBundle): CandidateView[] {
  const candidatePayload = payload(bundle, "candidate_set");
  return items(candidatePayload.candidates ?? candidatePayload.items).map((candidate) => ({
    id: text(candidate.candidate_id ?? candidate.id, "unknown-candidate"),
    title: text(candidate.title ?? candidate.name, "이름 없는 후보"),
    summary: text(candidate.summary ?? candidate.description, "설명 없음"),
  }));
}

export function criteriaView(bundle: DecisionViewBundle): CriterionView[] {
  const criteriaPayload = payload(bundle, "criteria_set");
  return items(criteriaPayload.criteria ?? criteriaPayload.items).map((criterion) => ({
    id: text(criterion.criterion_id ?? criterion.id, "unknown-criterion"),
    title: text(criterion.title ?? criterion.name, "이름 없는 기준"),
    definition: text(criterion.definition ?? criterion.description, "정의 없음"),
    priority: text(criterion.priority, "unspecified"),
  }));
}

export function evaluationsView(bundle: DecisionViewBundle): EvaluationView[] {
  const evaluationPayload = payload(bundle, "evaluation_set");
  const comparisonPayload = payload(bundle, "comparison");
  const rawItems =
    evaluationPayload.cells ??
    evaluationPayload.evaluations ??
    comparisonPayload.cells ??
    comparisonPayload.matrix;
  return items(rawItems).map((evaluation) => ({
    candidateId: text(evaluation.candidate_id, "unknown-candidate"),
    criterionId: text(evaluation.criterion_id, "unknown-criterion"),
    assessment: text(evaluation.effective_assessment ?? evaluation.assessment, "not_evaluated"),
    rationale: text(evaluation.rationale, "근거 설명 없음"),
    evidenceIds: texts(evaluation.evidence_ids ?? evaluation.evidence_refs),
  }));
}

function locatorText(value: unknown): string {
  const locator = record(value);
  const start = locator.start_line;
  const end = locator.end_line;
  if (Number.isSafeInteger(start) && Number.isSafeInteger(end)) {
    return `line ${String(start)}–${String(end)}`;
  }
  return "locator 없음";
}

export function evidenceView(bundle: DecisionViewBundle): EvidenceView[] {
  const evidencePayload = payload(bundle, "evidence_set");
  return items(evidencePayload.evidence ?? evidencePayload.evidences ?? evidencePayload.items).map(
    (evidence) => {
      const evidenceId = text(evidence.evidence_id ?? evidence.id, "unknown-evidence");
      const source = record(evidence.source);
      return {
        id: evidenceId,
        claim: text(evidence.claim, "claim 없음"),
        provenance: text(evidence.provenance, "unknown"),
        sourceId: text(source.source_id ?? evidence.source_id, "source 없음"),
        locator: locatorText(Object.keys(source).length ? source : evidence.locator),
        excerpt: bundle.cited_excerpts[evidenceId],
      };
    },
  );
}

export function recommendationView(bundle: DecisionViewBundle): {
  disposition: string;
  candidateId: string | null;
  rationale: string;
} | null {
  const recommendation = payload(bundle, "recommendation");
  if (Object.keys(recommendation).length === 0) return null;
  const candidate = recommendation.candidate_id ?? recommendation.selected_candidate_id;
  return {
    disposition: text(recommendation.disposition, candidate ? "select" : "abstain"),
    candidateId: typeof candidate === "string" ? candidate : null,
    rationale: text(recommendation.rationale ?? recommendation.reason, "설명 없음"),
  };
}

export function finalDecisionView(bundle: DecisionViewBundle): {
  disposition: string;
  candidateId: string | null;
  reason: string;
  relation: string;
  riskAcknowledgements: string[];
} | null {
  const decision = payload(bundle, "final_decision");
  if (Object.keys(decision).length === 0) return null;
  const candidate = decision.candidate_id ?? decision.selected_candidate_id;
  return {
    disposition: text(decision.disposition),
    candidateId: typeof candidate === "string" ? candidate : null,
    reason: text(decision.reason ?? decision.rationale, "설명 없음"),
    relation: text(decision.recommendation_relation, "no_recommendation"),
    riskAcknowledgements: texts(decision.risk_acknowledgements),
  };
}

export function mustEligibility(bundle: DecisionViewBundle): Record<string, boolean> {
  const comparison = payload(bundle, "comparison");
  const direct = record(comparison.must_eligibility);
  const result: Record<string, boolean> = {};
  for (const [candidateId, eligible] of Object.entries(direct)) {
    if (typeof eligible === "boolean") result[candidateId] = eligible;
  }
  const eligibleCandidates = texts(
    comparison.eligible_candidate_ids ?? comparison.eligible_candidates,
  );
  for (const candidateId of eligibleCandidates) result[candidateId] = true;
  const ineligibleCandidates = texts(comparison.ineligible_candidate_ids);
  for (const candidateId of ineligibleCandidates) result[candidateId] = false;
  return result;
}
