# v2 Data Contract

JSON Schema Draft 2020-12 파일은 `src/ai_work_harness/schemas/v2/`가 소유한다. Python
runtime 모델은 frozen dataclass와 Enum/문자열 literal을 사용하며 Pydantic에 의존하지
않는다. 문서보다 schema와 core 검증이 우선한다.

## Canonical JSON

- UTF-8 I-JSON object만 허용한다.
- 중복 key, lone surrogate, NaN/Infinity, float를 거부한다.
- 정수는 JavaScript safe integer 범위(±2^53−1) 안이어야 한다.
- digest는 RFC 8785 JCS bytes의 SHA-256 소문자 hex다.
- envelope에는 self-hash를 넣지 않는다. object 주소가 digest다.

artifact envelope의 정확한 top-level field는 다음 여섯 개다.

```json
{
  "schema_version": "2.0",
  "artifact_type": "criteria-set",
  "session_id": "triage-demo",
  "producer": {"kind": "local_operator"},
  "parents": {"candidate_set": "<sha256>"},
  "payload": {}
}
```

사용자 import JSON에는 `payload` 내용만 둔다. session binding, producer, event ID, digest,
timestamp, identity field는 core가 주입한다.

snapshot ref와 artifact parent의 key는 artifact type 문자열이 아니라 underscore를 쓰는
논리적 역할 이름이다. 예를 들어 `candidate_set`은 `candidate-set` artifact만 참조할 수 있다.
core의 단일 registry가 각 ref key를 기대 artifact type에 대응시키며, 선언되지 않은 key,
snapshot ref의 type mismatch, parent role의 type mismatch를 모두 무결성 오류로 거부한다.

## 주소와 ID

- 사람이 정하는 session/source/candidate/criterion/evidence ID:
  `^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`
- event ID: UUID4
- digest: `^[0-9a-f]{64}$`
- 시간: UTC RFC 3339 `Z`; test는 주입된 Clock과 IdSource만 사용한다.

logical ID로 경로를 만들기 전에 안전한 패턴을 검증하며 symlink와 path traversal을
거부한다.

## Public artifacts

| Type | 핵심 계약 |
|---|---|
| `source-manifest` | source ID, media type, byte count, raw blob digest; 원래 경로·timestamp·preview 없음 |
| `decision-frame` | 사용자 원문과 AI 초기 해석을 분리하고 business user, blocked decision, scope, assumptions 기록 |
| `candidate-set` | 안정적 ID를 가진 후보 최소 2개와 장점·단점·위험·불확실성 |
| `criteria-set` | 기준 최소 1개와 Must 최소 1개; priority만 허용하며 weight 금지 |
| `evidence-set` | claim, provenance, 1-based inclusive source locator와 excerpt byte hash |
| `evaluation-set` | 모든 활성 candidate × criterion에 정확히 한 cell |
| `evaluation-review-set` | AI/fixture Must·High cell의 concur/override/request_revision |
| `comparison` | 안정된 순서, 결과 matrix, evidence count, Must 적격성; scoring은 null |
| `recommendation` | Must 적격 후보 한 개 또는 abstain; final decision이 아님 |
| `final-decision` | select/reject_all/defer/request_more_evidence와 위험 수용 |
| `human-confirmation` | artifact digest challenge 결과; identity_verified는 항상 false |
| `approval-challenge` | bundle, source snapshot, nonce, proposed disposition, `issued_at`, `expires_at` binding |
| `human-approval` | challenge와 bundle digest, disposition, reason에 binding |
| `decision-bundle` | approval 대상 artifact digest map |
| `agent-run` | 성공한 model/prompt/input/outbound/transcript/result binding과 usage |
| `migration-report` | v1 fingerprint, copied input과 승격하지 않은 gate 기록 |
| `decision-view.v1` | sanitized offline viewer export와 integrity digest |

`approval-challenge`의 `expires_at`은 core가 `issued_at + 10분`으로 계산한다. approval의
`approved_at`은 `issued_at <= approved_at < expires_at`이어야 하므로 발급 시각보다 이른
clock과 만료 시각부터의 commit은 거부된다. approval snapshot은 challenge snapshot의 직접
후속이어야 하며 challenge의 disposition, bundle, nonce도 다시 일치해야 한다.

## Doctor liveness

object CAS는 프로젝트의 모든 v2 session이 공유하지만 snapshot은 session별 저장소에 있다.
`decision doctor`는 모든 session의 무결성이 유효한 current chain을 확인해 object liveness를
계산한다. 따라서 다른 정상 session이 참조하는 object를 현재 session의 orphan으로 오인하지
않으며, 손상된 session chain은 object를 live로 만들지 못하고 무결성 issue로 보고된다.
snapshot reachability와 orphan snapshot 판정은 선택한 session 범위에 머문다. orphan은
보고만 하며 자동 GC하지 않는다.

## Evidence와 evaluation

Phase 1 source는 최대 10 MiB UTF-8 `text/plain`, `text/markdown`이다. locator는
1-based inclusive line range이며 해당 줄의 원본 bytes hash를 가진다.

평가는 `meets`, `partial`, `fails`, `insufficient_evidence`, `not_applicable` 중 하나다.
`insufficient_evidence` 이외 결과에는 적어도 하나의 `source_observation` 참조가 필요하다.
그 밖의 provenance인 `user_assertion`과 `agent_inference`는 사실 근거로 승격되지 않는다.

AI/fixture가 만든 Must·High cell은 인간의 `concur` 또는 근거가 있는 `override`가 있어야
완료된다. Must는 effective assessment가 `meets`일 때만 통과하며 final approval에서
우회할 수 없다.

## Viewer export

export는 snapshot digest, sanitized artifact envelope, stale reason과 digest map을 담는다.
`integrity_sha256`는 해당 field를 제외한 top-level JCS payload의 SHA-256이다. 기본 export는
raw blob, source path, excerpt와 secret을 포함하지 않고 `cited_excerpts`를 빈 object로
남긴다. `--include-cited-excerpts`를 명시하면 evidence ID를 key로 하는 인용문 map을
추가하며, 이 opt-in은 합성·공개 가능한 입력에만 사용해야 한다.
