# Guided 합성 데모 transcript — 한국어

이 문서는 2026-08-12 KST에 다음 명령으로 관찰한 긴 터미널 출력을 핵심 checkpoint만 남겨
정리한 curated transcript다.

```bash
.venv/bin/python scripts/run_synthetic_decision_demo.py \
  --test-operator \
  --lang ko
```

입력은 `examples/decision-triage/`의 합성 파일뿐이며 외부 provider 호출은 없었다. 이 실행의
`--test-operator`는 재현 가능한 CI 입력 장치다. 실제 사람이 아니며 인간 신원, 수동 검토
또는 인증된 승인의 증거가 아니다.

전체 snapshot·artifact digest, UUID, nonce와 timestamp는 실행마다 달라질 수 있어 생략했다.
화면에서 사람이 대조하는 12자리 fingerprint만 `<fingerprint>`로 표시한다.

## 1. 하나의 guide invocation

```text
Synthetic data only. No real customer data is used.
Session: triage-demo
The injected test operator is deterministic CI input,
not human identity or human-review evidence.

의사결정 세션 'triage-demo'을(를) 만들까요? yes
의사결정 세션 'triage-demo'을(를) 만들었습니다.

AI Work Harness · 가이드 의사결정
상태: 원문 캡처
검증됨: 예
```

이후 raw mutation 명령을 호출하거나 cryptographic value를 붙여 넣지 않고 같은 guide
controller가 현재 snapshot을 내부에서 전달했다.

## 2. 사람이 의미를 확인하는 세 gate

```text
검증된 frame 요약
문제: 소규모 한국어 고객문의 triage 운영 방식을 선택한다.
Artifact fingerprint: <fingerprint>
선택: 이 frame 확인

검증된 candidates 요약
후보: classical-ml, llm-assisted, rules
Artifact fingerprint: <fingerprint>
선택: 이 candidates 확인

검증된 criteria 요약
Must: auditability, privacy
High: classification-quality
Medium: latency, operating-cost
Artifact fingerprint: <fingerprint>
선택: 이 criteria 확인
```

## 3. Must·High cell 9개 검토

guide는 다음 cell을 안정된 순서로 하나씩 보여주고 인용 근거, 평가, 신뢰도를 함께
표시했다. test operator는 9개 모두 `concur`와 비어 있지 않은 이유를 입력했다.

| 순서 | 후보 | 기준 | 우선순위 | fixture 평가 | 검토 |
|---:|---|---|---|---|---|
| 1 | `classical-ml` | auditability | Must | meets | concur |
| 2 | `classical-ml` | classification-quality | High | meets | concur |
| 3 | `classical-ml` | privacy | Must | meets | concur |
| 4 | `llm-assisted` | auditability | Must | partial | concur |
| 5 | `llm-assisted` | classification-quality | High | meets | concur |
| 6 | `llm-assisted` | privacy | Must | fails | concur |
| 7 | `rules` | auditability | Must | meets | concur |
| 8 | `rules` | classification-quality | High | partial | concur |
| 9 | `rules` | privacy | Must | meets | concur |

```text
완료한 검토를 기록할까요? yes
상태: 결정적 비교
검증됨: 예
```

`classical-ml`과 `rules`만 모든 Must를 통과했다. `llm-assisted`의 Must 실패는 점수나
자동 winner로 우회되지 않았다.

## 4. 추천과 인간 결정의 불일치

```text
AI 추천: select classical-ml

사람 최종 결정
처리: select
후보: rules
결정 이유: Rules are the simplest reviewed operating choice for this small team.

[확인할 위험]
rules / classification-quality
rules / latency
rules / operating-cost

추천과의 관계: different
이 최종 결정을 기록할까요? yes
```

저장된 final decision은 다음 의미를 가진다.

```text
Fixture recommendation: classical-ml
Human final selection: rules
Recommendation relation: different
Risk acknowledgements:
  - rules/classification-quality
  - rules/latency
  - rules/operating-cost
```

## 5. exact approval과 current export

guide는 full bundle digest, challenge ID와 nonce를 노출하거나 다시 입력받지 않았다. 운영자는
화면에 표시된 대상과 12자리 fingerprint가 포함된 문구를 정확히 다시 입력했다.

```text
[승인할 결정 요약]
처리: select · 후보: Rules [rules]
추천과의 관계: different
AI 추천: select classical-ml

최종 승인
Decision bundle fingerprint: <fingerprint>
정확히 입력하세요: APPROVE rules <fingerprint>
> APPROVE rules <fingerprint>
승인 이유: Reviewed the complete synthetic decision bundle.

결과: approval=approved, final=select, ready=true
선택: Viewer JSON 내보내기
Viewer export 완료.
```

`--snapshot`을 지정하지 않은 export는 당시 검증된 current snapshot을 고정했다.

```text
Exported snapshot:
  verified: true
  decision_complete: true
  ready: true
  recommendation candidate: classical-ml
  final candidate: rules
  recommendation relation: different
```

## 6. 같은 invocation에서 criteria 변경

export 뒤에도 terminal 메뉴로 돌아와 같은 guide invocation에서 criteria 변경을 선택했다.

```text
선택: 변경
변경할 항목: criteria_set
변경 시 stale 되는 활성 산출물:
  comparison, criteria_confirmation, decision_bundle,
  evaluation_review_set, evaluation_set, evidence_set,
  final_decision, human_approval, recommendation
이 변경을 계속할까요? yes

JSON payload 경로: examples/decision-triage/criteria.changed.json
선택: 종료하고 나중에 계속하기
```

변경 뒤 current pointer는 새 criteria draft를 가리켰고 과거 승인 object는 immutable CAS에
남았다.

```text
Current snapshot:
  verified: true
  decision_complete: false
  ready: false
  stale_reasons:
    - criteria_set_changed
```

따라서 승인된 export는 변경 전 결정을 재현하고, 현재 session은 변경된 criteria의 재확인부터
안전하게 재개된다.
