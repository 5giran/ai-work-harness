import {
  computeArtifactDigest,
  computeBundleIntegrity,
} from "../src/integrity";
import type {
  ArtifactEnvelope,
  ArtifactKey,
  DecisionViewBundle,
} from "../src/types";

const SNAPSHOT_DIGEST = "a".repeat(64);

function artifact(
  artifactKey: ArtifactKey,
  payload: Record<string, unknown>,
): ArtifactEnvelope {
  return {
    schema_version: "2.0",
    artifact_type: artifactKey.replaceAll("_", "-"),
    session_id: "support-triage",
    producer: { kind: "fixture" },
    parents: {},
    payload,
  };
}

export async function makeValidBundle(
  options: { includeExcerpts?: boolean; staleReasons?: string[] } = {},
): Promise<DecisionViewBundle> {
  const includeExcerpts = options.includeExcerpts ?? false;
  const artifacts: DecisionViewBundle["artifacts"] = {
    decision_frame: artifact("decision_frame", {
      business_user: "고객지원 운영 책임자",
      blocked_decision: "문의 분류 방식을 결정하지 못함",
      problem_statement: "감사 가능성과 개인정보 보호를 지키며 문의를 분류한다.",
    }),
    candidate_set: artifact("candidate_set", {
      candidates: [
        {
          candidate_id: "rules",
          title: "규칙 기반",
          summary: "명시적인 분류 규칙을 운영한다.",
        },
        {
          candidate_id: "classical-ml",
          title: "전통 ML",
          summary: "로컬 분류 모델과 검토 큐를 사용한다.",
        },
      ],
    }),
    criteria_set: artifact("criteria_set", {
      criteria: [
        {
          criterion_id: "privacy",
          title: "개인정보 보호",
          definition: "원문이 승인되지 않은 외부 시스템으로 나가지 않는다.",
          priority: "must",
        },
        {
          criterion_id: "quality",
          title: "분류 품질",
          definition: "합성 검증셋에서 오류 유형을 추적할 수 있다.",
          priority: "high",
        },
      ],
    }),
    evidence_set: artifact("evidence_set", {
      evidence: [
        {
          evidence_id: "ev-local",
          claim: "규칙 기반 처리는 로컬 환경에서 실행된다.",
          provenance: "source_observation",
          source: {
            source_id: "synthetic-ops",
            start_line: 2,
            end_line: 4,
            excerpt_sha256: "b".repeat(64),
          },
        },
      ],
    }),
    evaluation_set: artifact("evaluation_set", {
      cells: [
        {
          candidate_id: "rules",
          criterion_id: "privacy",
          assessment: "meets",
          rationale: "외부 전송이 없다.",
          evidence_ids: ["ev-local"],
        },
        {
          candidate_id: "rules",
          criterion_id: "quality",
          assessment: "partial",
          rationale: "규칙 누락을 지속 검토해야 한다.",
          evidence_ids: ["ev-local"],
        },
        {
          candidate_id: "classical-ml",
          criterion_id: "privacy",
          assessment: "meets",
          rationale: "모델을 로컬에서 실행한다.",
          evidence_ids: ["ev-local"],
        },
        {
          candidate_id: "classical-ml",
          criterion_id: "quality",
          assessment: "meets",
          rationale: "검증셋으로 오류를 추적한다.",
          evidence_ids: ["ev-local"],
        },
      ],
    }),
    comparison: artifact("comparison", {
      eligible_candidate_ids: ["rules", "classical-ml"],
      ineligible_candidate_ids: [],
    }),
    recommendation: artifact("recommendation", {
      disposition: "select",
      candidate_id: "classical-ml",
      rationale: "합성 평가에서 분류 품질 근거가 더 충분하다.",
    }),
    final_decision: artifact("final_decision", {
      disposition: "select",
      candidate_id: "rules",
      reason: "현재 팀은 규칙 변경을 직접 감사하는 방식을 우선한다.",
      recommendation_relation: "different",
      risk_acknowledgements: ["분류 품질은 정기적으로 재검증한다."],
    }),
  };

  const digestEntries = await Promise.all(
    Object.entries(artifacts).map(async ([type, envelope]) => [
      type,
      await computeArtifactDigest(envelope),
    ]),
  );

  const unsigned: Omit<DecisionViewBundle, "integrity_sha256"> = {
    schema_version: "decision-view.v1",
    snapshot_sha256: SNAPSHOT_DIGEST,
    generated_at: "2026-08-11T03:00:00Z",
    include_cited_excerpts: includeExcerpts,
    status: {
      lifecycle_state: options.staleReasons?.length ? "stale" : "approved",
      verified: !options.staleReasons?.length,
      decision_complete: !options.staleReasons?.length,
      ready: !options.staleReasons?.length,
      stale_reasons: options.staleReasons ?? [],
    },
    artifacts,
    digest_map: Object.fromEntries(digestEntries) as DecisionViewBundle["digest_map"],
    cited_excerpts: includeExcerpts
      ? { "ev-local": "합성 데이터만 사용하는 로컬 실행 기록" }
      : {},
  };

  return {
    ...unsigned,
    integrity_sha256: await computeBundleIntegrity(unsigned),
  };
}

export async function resignBundle(bundle: DecisionViewBundle): Promise<DecisionViewBundle> {
  const { integrity_sha256: _discarded, ...unsigned } = bundle;
  return {
    ...unsigned,
    integrity_sha256: await computeBundleIntegrity(unsigned),
  };
}
