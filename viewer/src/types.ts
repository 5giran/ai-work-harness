export const ARTIFACT_KEYS = [
  "source_manifest",
  "decision_frame",
  "candidate_set",
  "criteria_set",
  "evidence_set",
  "evaluation_set",
  "evaluation_review_set",
  "comparison",
  "recommendation",
  "final_decision",
  "human_approval",
] as const;

export type ArtifactKey = (typeof ARTIFACT_KEYS)[number];

export interface ArtifactEnvelope {
  schema_version: "2.0";
  artifact_type: string;
  session_id: string;
  producer: { kind: string };
  parents: Record<string, string>;
  payload: Record<string, unknown>;
}

export interface DecisionViewStatus {
  lifecycle_state: string;
  verified: boolean;
  decision_complete: boolean;
  ready: boolean;
  stale_reasons: string[];
}

export interface DecisionViewBundle {
  schema_version: "decision-view.v1";
  snapshot_sha256: string;
  generated_at: string;
  include_cited_excerpts: boolean;
  status: DecisionViewStatus;
  artifacts: Record<string, ArtifactEnvelope>;
  digest_map: Record<string, string>;
  cited_excerpts: Record<string, string>;
  integrity_sha256: string;
}

export interface CandidateView {
  id: string;
  title: string;
  summary: string;
}

export interface CriterionView {
  id: string;
  title: string;
  definition: string;
  priority: string;
}

export interface EvaluationView {
  candidateId: string;
  criterionId: string;
  assessment: string;
  rationale: string;
  evidenceIds: string[];
}

export interface EvidenceView {
  id: string;
  claim: string;
  provenance: string;
  sourceId: string;
  locator: string;
  excerpt?: string;
}
