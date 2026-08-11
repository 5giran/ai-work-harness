# Synthetic demo transcript

이 기록은 2026-08-11 KST에 macOS/Python 3.12에서 다음 명령으로 실행한 합성 workflow의
요약이다.

```bash
.venv/bin/python scripts/run_synthetic_decision_demo.py \
  --cli .venv/bin/ai-work-harness \
  --root /tmp/ai-work-harness-synthetic-demo-docs-3 \
  --test-operator
```

`--test-operator`가 digest challenge 값을 자동 입력했으므로 이 기록은 실제 인간 신원 또는
수동 확인의 증거가 아니다. 입력은 `examples/decision-triage/`의 합성 파일뿐이고 외부
provider 호출은 없었다.

## 관찰된 결과

```text
Fixture recommendation: classical-ml
Human final selection: rules
Recommendation relation: different
Must-eligible candidates: classical-ml, rules
Approved snapshot: c8ca8375588a0f83ec67addadffcf92b55e2507f771822d7b3764ccff99fd55d
Approved snapshot verify: true
Approved snapshot ready: true
```

그 뒤 `classification-quality` 정의가 더 엄격한 `criteria.changed.json`을 import했다.

```text
Current snapshot verify: true
Current snapshot ready: false
Stale reasons:
  - criteria_set_changed
  - evidence_changed
  - evaluations_changed
  - reviews_changed
  - comparison_changed
  - recommendation_changed
  - final_decision_changed
```

approval과 downstream active ref는 제거됐고, 과거 object 자체는 immutable CAS에 남았다.
run-specific UUID, nonce, timestamp와 digest는 실행마다 달라질 수 있다. CI의 주입된
Clock/IdSource service test가 같은 상태 전이의 결정적 fixture를 별도로 검증한다.
