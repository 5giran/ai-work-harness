# Operator Runbook

## 설치와 진단

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ai-work-harness --version
```

OpenAI 또는 stdio MCP integration이 필요할 때만 `.[openai]`, `.[mcp]` extra를 설치한다.
실제 source를 외부 provider에 보내기 전 별도 동의가 필요하다.

## 새 v2 session

```bash
HARNESS=.venv/bin/ai-work-harness
ROOT=/absolute/path/to/local-project
SESSION=triage-decision

$HARNESS --root "$ROOT" decision init --session-id "$SESSION"
```

출력의 `snapshot_sha256`를 다음 mutation의 `--expected-parent`로 사용한다. 축약 digest나
자동 감지는 지원하지 않는다. 매 mutation 뒤 새 digest를 갱신한다.

## 표준 흐름

1. `decision source capture`: 최대 10 MiB UTF-8 source를 CAS에 캡처한다.
2. `frame import` 후 status의 `decision_frame` digest를 직접 확인하고 `frame confirm`한다.
3. candidates와 criteria도 각각 import, digest 대조, confirm한다.
4. evidence locator의 line range와 raw byte hash를 만든 뒤 import한다.
5. evaluations를 import/generate하고 AI/fixture Must·High cell을 사람이 review한다.
6. `compare`로 정성 matrix와 Must 적격성을 계산한다.
7. 선택적으로 recommendation을 기록한다. 추천은 final decision이 아니다.
8. final decision을 import한다. 선택 후보의 unresolved High와 미검토 AI Medium·Low cell을
   `risk_acknowledgements`에 빠짐없이 넣는다.
9. approval challenge를 만든 뒤 10분 안에 전체 challenge ID, nonce, bundle digest를 다시
   입력해 commit한다.
10. 같은 snapshot을 명시해 verify하고 status의 ready를 확인한다.

전체 명령 예시는 `scripts/run_synthetic_decision_demo.py`가 생성·실행한다. 입력 JSON은
`examples/decision-triage/`에서 볼 수 있다.

## Status 해석

- `verified=false`: schema/digest/parent/workflow integrity 문제를 먼저 해결한다.
- `decision_complete=true`, `ready=false`: reject_all이 승인됐거나 final disposition이 select가
  아니거나 verify가 실패한 상태일 수 있다.
- `ready=true`: 이 pinned snapshot의 승인된 select와 전체 verify만 뜻한다. 운영 배포 승인,
  사용자 인증, 모델 품질 보장은 아니다.
- `stale_reasons`: 어떤 upstream 변경으로 downstream 활성 ref가 제거됐는지 설명한다.

## 충돌과 장애

| Error / 상태 | 조치 |
|---|---|
| `WRITE_CONFLICT` | current status를 다시 읽고 의도한 mutation을 새 parent에 재검토 |
| `WRITE_LOCK_TIMEOUT` | 다른 writer가 끝났는지 확인; lock 파일을 임의 삭제하기 전에 process 조사 |
| integrity exit `5` | 쓰기를 중단하고 `decision doctor`, pinned verify, 백업 비교 수행 |
| provider exit `4` | current pointer가 그대로인지 확인; retryable 조건만 정책에 따라 재시도 |
| orphan object | 활성 pointer에 영향이 없음을 doctor로 확인; 자동 GC 없음 |

## Migration

```bash
$HARNESS --root "$ROOT" decision migrate-v1 --session-id migrated-v1
```

명령 전후 v1 tree는 수정되지 않아야 한다. migration report에서 fingerprint, copied source,
`confirmations_promoted=false`, `approvals_promoted=false`를 확인한다. migration 이후 v1 변경은
기존 v2 session에 자동 반영되지 않는다.

## Viewer export

```bash
$HARNESS --root "$ROOT" decision export-view \
  --session-id "$SESSION" \
  --snapshot <full-snapshot-sha256> \
  --output /absolute/path/decision-view.v1.json
```

viewer를 열어 file picker 또는 drag-and-drop으로 한 파일을 읽는다. integrity/schema 실패
화면이 나오면 내용을 신뢰하거나 수동으로 우회하지 않는다.

기본 export의 `cited_excerpts`는 비어 있다. 인용문이 합성·공개 가능하며 노출해도 안전함을
운영자가 확인한 때만 `--include-cited-excerpts`를 명시한다. 이 option은 raw source 전체를
포함하는 허가가 아니다.
