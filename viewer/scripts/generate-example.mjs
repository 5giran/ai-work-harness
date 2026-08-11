import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import canonicalize from "canonicalize";

const viewerRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const outputPath = resolve(viewerRoot, "public/examples/synthetic-decision-view.v1.json");
const digest = (value) => createHash("sha256").update(canonicalize(value), "utf8").digest("hex");
const envelope = (artifactType, payload, parents = {}) => ({
  schema_version: "2.0",
  artifact_type: artifactType.replaceAll("_", "-"),
  session_id: "support-triage",
  producer: { kind: "fixture" },
  parents,
  payload,
});

const artifacts = {
  decision_frame: envelope("decision_frame", {
    business_user: "고객지원 운영 책임자",
    blocked_decision: "문의 분류 방식을 결정하지 못함",
    problem_statement: "감사 가능성과 개인정보 보호를 지키며 문의를 분류한다.",
  }),
  candidate_set: envelope("candidate_set", {
    candidates: [
      {
        candidate_id: "rules",
        title: "규칙 기반",
        summary: "명시적인 분류 규칙을 운영하고 변경 내역을 직접 감사한다.",
      },
      {
        candidate_id: "classical-ml",
        title: "전통 ML",
        summary: "로컬 분류 모델과 인간 검토 큐를 함께 사용한다.",
      },
      {
        candidate_id: "llm-assisted",
        title: "LLM 보조",
        summary: "모델 초안과 인간 검토를 결합한다.",
      },
    ],
  }),
  criteria_set: envelope("criteria_set", {
    criteria: [
      {
        criterion_id: "privacy",
        title: "개인정보 보호",
        definition: "승인되지 않은 외부 시스템으로 문의 원문을 보내지 않는다.",
        priority: "must",
      },
      {
        criterion_id: "auditability",
        title: "감사 가능성",
        definition: "결정 근거와 변경 이력을 사람이 재검토할 수 있다.",
        priority: "must",
      },
      {
        criterion_id: "quality",
        title: "분류 품질",
        definition: "합성 검증셋의 오류 유형을 추적하고 개선할 수 있다.",
        priority: "high",
      },
    ],
  }),
  evidence_set: envelope("evidence_set", {
    evidence: [
      {
        evidence_id: "ev-local-run",
        claim: "규칙 및 전통 ML 후보는 로컬 환경에서 문의를 처리한다.",
        provenance: "source_observation",
        source: {
          source_id: "synthetic-ops-note",
          start_line: 2,
          end_line: 4,
          excerpt_sha256: "b".repeat(64),
        },
      },
      {
        evidence_id: "ev-change-log",
        claim: "규칙 변경 기록에는 작성자와 검토 사유가 포함된다.",
        provenance: "source_observation",
        source: {
          source_id: "synthetic-change-log",
          start_line: 7,
          end_line: 9,
          excerpt_sha256: "c".repeat(64),
        },
      },
    ],
  }),
  evaluation_set: envelope("evaluation_set", {
    cells: [
      ["rules", "privacy", "meets", "로컬 실행 근거가 있다.", ["ev-local-run"]],
      ["rules", "auditability", "meets", "변경 기록을 직접 감사할 수 있다.", ["ev-change-log"]],
      ["rules", "quality", "partial", "규칙 누락을 정기 검토해야 한다.", ["ev-change-log"]],
      ["classical-ml", "privacy", "meets", "모델이 로컬에서 실행된다.", ["ev-local-run"]],
      ["classical-ml", "auditability", "partial", "오류 분석 기록이 추가로 필요하다.", ["ev-change-log"]],
      ["classical-ml", "quality", "meets", "검증셋으로 오류 유형을 추적한다.", ["ev-local-run"]],
      ["llm-assisted", "privacy", "fails", "외부 전송 동의 근거가 없다.", ["ev-local-run"]],
      ["llm-assisted", "auditability", "partial", "모델 설명은 인간 검토가 필요하다.", ["ev-change-log"]],
      ["llm-assisted", "quality", "insufficient_evidence", "실측 근거가 없다.", []],
    ].map(([candidate_id, criterion_id, assessment, rationale, evidence_ids]) => ({
      candidate_id,
      criterion_id,
      assessment,
      rationale,
      evidence_ids,
    })),
  }),
  comparison: envelope("comparison", {
    eligible_candidate_ids: ["rules", "classical-ml"],
    ineligible_candidate_ids: ["llm-assisted"],
  }),
  recommendation: envelope("recommendation", {
    disposition: "select",
    candidate_id: "classical-ml",
    rationale: "합성 평가에서는 분류 품질 근거가 더 충분하다.",
  }),
  final_decision: envelope("final_decision", {
    disposition: "select",
    candidate_id: "rules",
    reason: "현재 팀은 규칙 변경을 직접 감사하는 방식을 우선한다.",
    recommendation_relation: "different",
    risk_acknowledgements: ["분류 품질은 정기적으로 재검증한다."],
  }),
};

const unsignedBundle = {
  schema_version: "decision-view.v1",
  snapshot_sha256: "a".repeat(64),
  generated_at: "2026-08-11T03:00:00Z",
  include_cited_excerpts: false,
  status: {
    lifecycle_state: "approved",
    verified: true,
    decision_complete: true,
    ready: true,
    stale_reasons: [],
  },
  artifacts,
  digest_map: Object.fromEntries(
    Object.entries(artifacts).map(([artifactType, artifact]) => [artifactType, digest(artifact)]),
  ),
  cited_excerpts: {},
};
const bundle = { ...unsignedBundle, integrity_sha256: digest(unsignedBundle) };
const generated = `${JSON.stringify(bundle, null, 2)}\n`;

let existing = "";
try {
  existing = await readFile(outputPath, "utf8");
} catch (error) {
  if (!(error && typeof error === "object" && "code" in error && error.code === "ENOENT")) {
    throw error;
  }
}
if (existing !== generated) await writeFile(outputPath, generated, "utf8");
