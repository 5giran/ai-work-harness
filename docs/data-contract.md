# v2 Data Contract

JSON Schema Draft 2020-12 파일은 `src/ai_work_harness/schemas/v2/`가 소유한다. Python
runtime 모델은 frozen dataclass와 Enum/문자열 literal을 사용하며 Pydantic에 의존하지
않는다. 문서보다 schema와 core 검증이 우선한다. v0.5의 guided TTY와 raw CLI/MCP는 별도
저장 형식을 만들지 않고 같은 v2 artifact envelope, payload schema와 ref registry를 사용한다.

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
  "parents": {
    "candidate_set": "<sha256>",
    "candidate_confirmation": "<sha256>"
  },
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
| `human-confirmation` | decision artifact 또는 outbound manifest에 대한 인간 확인; method별 subject 제약이 있으며 identity_verified는 항상 false |
| `approval-challenge` | bundle, source snapshot, nonce, proposed disposition, `issued_at`, `expires_at` binding |
| `human-approval` | challenge와 bundle digest, disposition, reason에 binding |
| `decision-bundle` | approval 대상 artifact digest map |
| `agent-run` | 성공한 model/prompt/input/outbound/transcript/result binding과 usage |
| `migration-report` | v1 fingerprint, copied input과 승격하지 않은 gate 기록 |
| `decision-view.v1` | sanitized offline viewer export와 integrity digest |

### Human confirmation method

`human-confirmation.method`는 다음 세 값만 허용한다.

| Method | 허용 subject | 인터페이스 의미 |
|---|---|---|
| `digest_challenge` | decision-frame/candidate-set/criteria-set 또는 outbound-manifest | raw CLI가 full artifact/manifest digest를 명시해 확인 |
| `guided_semantic_review` | decision-frame/candidate-set/criteria-set만 | guide가 전체 semantic summary와 fingerprint를 보여준 뒤 별도 confirmation snapshot 생성 |
| `guided_exact_phrase` | outbound-manifest만 | guide에서 `SEND OPENAI <12자 fingerprint>`가 정확히 일치한 뒤 full manifest digest에 binding |

세 method 모두 `actor_label=local_operator`, `identity_verified=false`, full
`subject_sha256`와 `confirmed_at`을 저장한다. outbound confirmation은 payload 안에 closed
`outbound_manifest` 전체도 저장하며 활성 snapshot에서는 `agent_consent` ref로 가리킨다.
`agent_consent`는 새 artifact type이 아니라 `human-confirmation`의 논리적 ref 역할이다.

raw와 guided는 confirmation method를 제외한 semantic payload와 활성 ref topology가
동등하다. method가 canonical payload 일부이므로 confirmation 이후 두 실행의 artifact와
snapshot digest는 달라질 수 있으며, 어느 한쪽 digest를 다른 인터페이스에 그대로 대입해서는
안 된다.

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

### Writer lock 진단

doctor 응답에는 `integrity_ok`와 `writer_lock`이 추가된다. 이는 저장 artifact가 아닌 일시적
진단 결과다. `ok`는 저장 무결성이 정상이고 선택한 session의 lock이 없을 때만 true다.
`integrity_ok`는 기존 `issues`가 비어 있는지를 뜻하며 workflow 도메인 검증 결과가 아니다.

`writer_lock`은 `state`, `pid`, `acquired_at`, `owner_status`, `issue`를 포함한다.
`state`는 `absent|present|invalid|unreadable|changed`이고, `owner_status`는
`pid_present|pid_absent|unknown`이다. metadata를 읽을 수 없으면 PID와 시각은 null이다.
POSIX PID 조회는 존재 여부만 관찰하며 Windows에서는 `unknown`을 반환한다. 자동 삭제하지
않는다. lock만 문제면 service doctor는 `WRITE_LOCK_PRESENT`(exit `3`)와 `details.doctor`를
반환하고 저장 무결성 오류가 함께 있으면 integrity 오류(exit `5`)를 우선한다.

## Pinned operator read model

`decision next`의 `operator-plan.v1`은 저장되는 artifact가 아니라 하나의 verified snapshot에서
결정적으로 파생되는 read model이다. raw envelope가 `session_id`, `generation`, full
`snapshot_sha256`를 운반하고 plan은 observed time, stage, recommended/available action,
sanitized summary, pending review, final constraint, public challenge 상태, readiness와 stale
reason을 담는다.

public challenge에는 status, disposition, issued/expiry time, remaining seconds와 12자 bundle
fingerprint만 있고 nonce, challenge ID, full bundle digest는 없다. raw source와 excerpt도
plan에 포함하지 않는다. 내부 `ValidatedOperatorState`는 active artifact를 immutable view로
보유하지만 저장 계약이 아니며, integrity failure가 있으면 plan을 부분 생성하지 않고
fail-closed한다.

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
