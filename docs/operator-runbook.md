# Operator Runbook

이 문서는 v0.5.0의 기본 guided workflow와 자동화용 raw protocol을 함께 설명한다. 기본
운영자는 로컬 한국어 사용자 한 명이며, 모든 명령의 project root와 session ID는 명시적이다.

## 1. 설치

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ai-work-harness --version
```

OpenAI 또는 stdio MCP가 필요할 때만 extra를 설치한다.

```bash
.venv/bin/pip install -e '.[openai,mcp]'
```

## 2. Guided workflow 시작과 재개

```bash
HARNESS=.venv/bin/ai-work-harness
ROOT=/absolute/path/to/local-project
SESSION=triage-decision

$HARNESS --root "$ROOT" decision guide "$SESSION"
```

- guide는 stdin과 stdout이 모두 TTY일 때만 실행된다.
- session이 없으면 root와 ID를 먼저 표시하고 `y/N`으로 생성 여부를 묻는다.
- 기존 session이면 current pointer를 한 번 읽고 그 snapshot을 fail-closed verify한 뒤 재개한다.
- 한국어가 기본이며 `--lang en`으로 영어를 선택한다.
- 전역 active session, home 설정 또는 숨은 draft 파일을 만들지 않는다.

명시적 `종료`는 exit `0`과 resumable generation/fingerprint를 출력한다. 같은 명령을 다시
실행하면 검증된 현재 단계부터 이어진다.

## 3. Guided 표준 흐름

### Source

최대 10 MiB UTF-8 text/Markdown 파일을 하나 이상 캡처한다. source ID와 media type은
artifact에 남지만 원래 path, timestamp와 preview는 저장하지 않는다. source가 하나 이상이면
추가 캡처 또는 frame 진행을 선택할 수 있다.

### Frame, candidates, criteria

각 항목은 다음 세 방식 중 가능한 것을 고른다.

- guided form
- 사용자 지정 JSON payload import
- 종료 후 MCP/agent가 draft를 기록하고 guide 재개

draft import와 인간 confirmation은 별도 snapshot이다. confirmation 화면은 전체 semantic
summary와 artifact fingerprint를 보여주며 `확인 / 수정 / 종료` 중 하나를 요구한다. guided
확인은 `guided_semantic_review`, raw digest 확인은 `digest_challenge` method로 기록된다.

확인을 취소하거나 종료하면 draft snapshot은 남고 confirmation은 만들어지지 않는다. 긴
guided form은 운영자가 명시한 외부 JSON path에만 저장할 수 있다. managed v2 저장소 내부,
symlink 또는 directory target은 거부한다.

### Evidence

`source_observation`은 source ID와 1-based inclusive line range를 입력한다. guide는 번호가
붙은 excerpt를 보여주고 원본 byte hash를 자동 계산한다. `user_assertion`과
`agent_inference`는 source 없이 별도 provenance로 기록되며 사실 근거로 승격되지 않는다.

### Evaluation과 인간 review

evaluation draft는 다음 경로를 지원한다.

- JSON import
- 결정적인 Fixture
- OpenAI
- 기존 MCP/agent draft를 기록한 뒤 guide 재개

수동 Cartesian matrix form은 제공하지 않는다. AI 또는 Fixture가 만든 모든 Must·High cell은
안정된 순서로 표시되며 사람은 다음 중 하나를 기록한다.

- `concur`: 표시된 assessment에 동의
- `override`: replacement assessment, 사실 evidence와 비어 있지 않은 reason 필요
- `request_revision`: 현재 review artifact에 불변 revision request 기록

`request_revision`은 미검토 cell이 남아도 현재까지의 review prefix를 저장할 수 있지만
comparison을 차단한다. 같은 evaluation에 review를 다시 써서 우회할 수 없다. 새 evaluation을
import/generate해야 하며, 새 evaluation은 이전 review를 stale 처리한다.

review가 완료되면 comparison은 별도 인간 질문 없이 core가 계산하고 즉시 보여준다. 점수,
weight 또는 자동 winner는 만들지 않는다.

### Recommendation과 final decision

comparison 뒤에는 Fixture/OpenAI/JSON recommendation 또는 recommendation 없는 final을
고를 수 있다. recommendation은 비구속적이며 자동 final·approval로 승격되지 않는다.

final 화면은 다음을 의미 중심으로 보여준다.

- Must 적격 후보
- AI 추천과 후보별 관계
- 선택 후보의 unresolved High cell
- 미검토 AI Medium·Low cell
- reject-all의 공통 위험 가능 조건

운영자가 확인한 위험은 core가 exact cell ID로 변환한다. 추천과 다른 후보 선택은 정상이며
`recommendation_relation=different`로 기록된다. `defer`와 `request_more_evidence`는 기록할 수
있지만 승인 완료 또는 ready 상태가 아니다.

### Approval

challenge 생성과 approval commit은 별도 snapshot이다. guide는 challenge ID, nonce, full
bundle digest를 사용자에게 복사시키지 않고 내부 전달한다. 대신 decision brief, disposition,
target과 bundle fingerprint를 보여주고 다음 형식의 정확 문구를 요구한다.

```text
APPROVE <candidate|REJECT-ALL> <fingerprint>
REJECT <candidate|REJECT-ALL> <fingerprint>
REQUEST-CHANGES <candidate|REJECT-ALL> <fingerprint>
```

reason은 비어 있을 수 없다. challenge는 10분 동안 유효하며 만료된 경우 운영자가 확인한 뒤
같은 disposition으로 새 challenge를 발급한다.

active human approval이 있는 동일 final에는 challenge를 중복 생성할 수 없다. `rejected` 또는
`changes_requested` 뒤에는 semantic final payload나 upstream artifact가 실제로 바뀌어야
재승인할 수 있다. timestamp만 달라진 동일 final import는 재승인 근거가 아니다.

### Export

완료 화면에서 viewer JSON을 내보내도 guide는 종료되지 않는다. export 뒤 변경 메뉴에서
upstream을 교체해 어떤 downstream이 stale 되는지 확인할 수 있다.

## 4. OpenAI outbound consent와 재개

guide에서 OpenAI를 고르면 네트워크 호출 전에 다음 항목만 보여준다.

- operation, provider, model
- prompt ID와 prompt fingerprint
- input fingerprint
- cited source excerpt byte 수
- evidence record 수
- outbound manifest fingerprint

raw source, excerpt 본문, source digest 전체 목록과 full manifest digest는 표시하지 않는다.
다음 문구가 정확히 일치해야 consent snapshot을 만든 뒤 외부 호출한다.

```text
SEND OPENAI <fingerprint>
```

guided outbound confirmation method는 `guided_exact_phrase`다. 모델, prompt, input 또는
source가 바뀌면 manifest가 달라지므로 이전 consent를 재사용할 수 없다. 목적지는
`https://api.openai.com/v1`로 고정되어 있다. `OPENAI_BASE_URL`은 미설정 또는 이 주소여야 하며
다른 주소는 preview·consent·run과 각 요청 전에 거부된다.

provider timeout, refusal 또는 transport 실패는 exit `4`이며 pointer와 active consent를
변경하지 않는다. 같은 guide 명령으로 재개하면 다음 중 하나를 명시적으로 선택한다.

- 저장된 동일 manifest로 provider run 재시도
- local JSON result import
- 종료 후 MCP/agent result 기록

재개 시 preview와 consent를 다시 만들지 않는다. consent snapshot에서 preview를 재계산하면
input snapshot binding이 달라지기 때문이다. 자동 retry, 자동 fallback과 provider 실패 뒤
자동 final decision은 없다.

## 5. 충돌과 실패 복구

| Error / 상태 | Guided 조치 |
|---|---|
| `WRITE_CONFLICT`, exit `3` | 실패한 mutation을 자동 재적용하지 않는다. 고정/current fingerprint를 확인하고 명시적으로 reload한 뒤 새 semantic state를 다시 검토한다. |
| `WRITE_LOCK_TIMEOUT`, exit `3` | 다른 writer process를 확인한다. 원인을 모른 채 lock 파일을 삭제하지 않는다. |
| `WRITE_LOCK_PRESENT`, exit `3` | doctor가 lock을 관찰했다. 오류의 `details.doctor`에서 무결성과 lock 상태를 따로 확인하고 아래 복구 절차를 따른다. |
| `UNSUPPORTED_OPENAI_ENDPOINT`, exit `4` | `OPENAI_BASE_URL`을 해제하거나 공식 주소로 복원한다. injected client도 공식 주소를 사용해야 한다. 실패 시 pointer와 consent는 유지된다. |
| `WORKFLOW_INTEGRITY_FAILED`, exit `5` | hash/schema가 맞아도 단계별 도메인 규칙이 틀린 저장 상태다. 오류의 `reason_code`를 확인하고 쓰기를 중단한다. |
| provider/transport exit `4` | pointer와 consent가 유지됐는지 확인하고 같은 manifest retry 또는 local/agent import를 선택한다. |
| integrity exit `5` | 모든 쓰기를 중단하고 `decision doctor`, pinned `verify`, 백업 비교를 수행한다. guide가 내용을 보여주도록 우회하지 않는다. |
| expired challenge | 화면에서 만료를 확인하고 새 challenge 발급 여부를 선택한다. |
| orphan object | doctor로 current graph에 미참조임을 확인한다. 자동 GC는 없다. |

### 강제 종료 뒤 writer lock 복구

1. 동일 root/session에 쓰는 CLI, guided session, MCP 및 자동 작업을 모두 중지하고 새 writer가
   시작되지 않도록 한다. 단지 명령이 timeout 났다는 이유로 lock을 옮기지 않는다.
2. `decision doctor --session-id "$SESSION"`을 실행한다. `integrity_ok`는 저장 무결성이고
   `writer_lock`은 별도 시점 관찰이다. `pid_present`는 PID 존재만 뜻하며 소유권을 증명하지
   않는다. `pid_absent`도 단독 복구 근거가 아니다. Windows 또는 접근 불가 시 `unknown`이다.
3. 프로세스 종료 기록 등으로 기존 writer가 끝났고 다른 writer도 없음을 독립적으로 확인한다.
   소유자나 파일 상태를 확인할 수 없다면 복구를 진행하지 않는다. lock 나이만으로 판단하지 않는다.
4. 확인을 마쳤다면 `.ai-work-harness/v2/sessions/<session-id>/writer.lock`을 managed 저장소
   **밖의 새 백업 경로**로 옮겨 보존한다. 기존 백업을 덮어쓰거나 object·snapshot·pointer를
   수정하지 않는다. 자동 해제·복구 명령은 제공하지 않는다.
5. doctor가 `ok=true`, `writer_lock.state=absent`인지 확인하고 `decision verify`도 실행한다.
   doctor의 저장 무결성 검사와 service의 workflow 검증은 별도이므로 둘 다 성공해야 한다.
   current snapshot을 다시 읽고 검토한 뒤 새 parent로 쓰기를 재개한다.

입력 종료 규칙:

- 명시적 종료: exit `0`
- `Ctrl-C`: exit `130`, stack trace 없음
- EOF: `INTERACTIVE_INPUT_CLOSED`, exit `2`
- non-TTY: session 생성 전 `INTERACTIVE_TERMINAL_REQUIRED`, exit `2`

prompt 도중 취소하면 아직 호출하지 않은 mutation은 없다. import가 성공한 뒤 confirmation을
취소하면 draft snapshot은 유지된다.

## 6. 상태와 `decision next`

자동화나 향후 UI는 raw refs를 직접 해석하지 않고 read-only plan을 사용할 수 있다.

```bash
$HARNESS --root "$ROOT" decision next --session-id "$SESSION"
```

`operator-plan.v1`은 다음을 포함한다.

- observed time, semantic stage, recommended/available actions
- source·artifact·producer의 민감정보 없는 summary
- 안정된 순서의 pending review
- candidate별 final constraint와 reject-all shared-risk 조건
- sanitized challenge 상태와 만료시각
- `decision_complete`, `ready`, stale reasons

nonce, full challenge binding, raw source와 excerpt는 포함하지 않는다. query는 current를 한 번만
읽고 동일 pinned snapshot을 verify하며 object, snapshot 또는 pointer를 쓰지 않는다. integrity
failure는 status처럼 축약하지 않고 exit `5`로 실패한다.

Status 의미:

- `decision_complete=true`: 검증된 approved event가 `select` 또는 `reject_all`을 승인했다.
- `ready=true`: `decision_complete`, final=`select`, 같은 pinned snapshot 전체 verify가 모두 참.
- `reject_all`, `rejected`, `changes_requested`, `defer`, `request_more_evidence`는 ready가 아니다.
- stale reason은 어떤 upstream 변경 때문에 이전 downstream 활성 ref가 제거됐는지 설명한다.

## 7. Advanced raw protocol

raw CLI는 자동화와 forensic 운영을 위한 공개 계약으로 유지된다. v2는 항상 `decision`
namespace를 사용한다.

```bash
$HARNESS --root "$ROOT" decision init --session-id "$SESSION"

$HARNESS --root "$ROOT" decision source capture \
  --session-id "$SESSION" \
  --expected-parent <full-current-snapshot-sha256> \
  --source-id brief \
  --file /absolute/path/brief.md
```

모든 raw mutation은 full `--expected-parent`를 요구한다. 성공 응답에서 새
`snapshot_sha256`를 읽어 다음 mutation에 전달한다. frame/candidate/criteria raw confirmation은
full `--expected-artifact-sha`를 요구한다.

OpenAI raw protocol은 세 경계를 유지한다.

```text
decision agent preview
decision agent consent --expected-manifest-sha <full-sha256>
decision evaluations generate --provider openai
```

approval raw commit도 full `challenge-id`, `nonce`, `expected-bundle-sha`와 reason file을
요구한다. guide가 이 값을 내부 전달한다고 raw 계약을 간소화하지 않는다.

Raw 정상 결과와 예상 오류는 다음 JSON envelope를 사용한다.

```json
{
  "ok": true,
  "command": "decision next",
  "session_id": "triage-decision",
  "generation": 8,
  "snapshot_sha256": "...",
  "result": {}
}
```

고정 exit code는 `0` 성공, `2` input/schema/policy, `3` stale/CAS/lock, `4` provider/transport,
`5` integrity, `1` 예상하지 못한 내부 오류다. help와 argparse syntax error만 text다.

## 8. Viewer export

검증된 current snapshot은 `--snapshot` 없이 내보낸다.

```bash
$HARNESS --root "$ROOT" decision export-view \
  --session-id "$SESSION" \
  --output /absolute/path/decision-view.v1.json
```

과거 snapshot을 조사할 때만 full digest를 지정한다.

```bash
$HARNESS --root "$ROOT" decision export-view \
  --session-id "$SESSION" \
  --snapshot <full-snapshot-sha256> \
  --output /absolute/path/historical-decision-view.v1.json
```

기본 export의 `cited_excerpts`는 비어 있다. 합성·공개 가능한 인용문임을 운영자가 확인한
때만 `--include-cited-excerpts`를 사용한다. viewer의 integrity/schema 실패 화면을 수동으로
우회하지 않는다.

## 9. v1 migration

```bash
$HARNESS --root "$ROOT" decision migrate-v1 --session-id migrated-v1
```

명령 전후 v1 tree는 byte-identical해야 한다. migration report에서 fingerprint, copied
source, `confirmations_promoted=false`, `approvals_promoted=false`를 확인한다. v1 frame은 nullable
draft일 뿐이며 migration 이후 v1 변경은 기존 v2 session에 자동 반영되지 않는다.

## 10. 합성 리허설

```bash
.venv/bin/python scripts/run_synthetic_decision_demo.py \
  --test-operator \
  --lang ko
```

한 guided invocation이 승인, viewer export와 criteria stale 전환까지 수행한다. 실제 사람이
진행하려면 `--test-operator`를 제거한다. `examples/decision-triage/`의 모든 입력은 합성이며
자동 test operator 기록은 실제 인간 신원 또는 검토 증거가 아니다.
