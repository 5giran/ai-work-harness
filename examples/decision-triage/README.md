# 합성 고객문의 triage 결정

외부 자료나 실제 고객 데이터를 포함하지 않는 v2 fixture 데모 입력이다. `rules`,
`classical-ml`, `llm-assisted`를 privacy, auditability, classification quality, latency,
operating cost로 비교한다. 결정적 fixture는 `classical-ml`을 추천하지만, 최종 결정은
위험을 명시하고 `rules`를 선택한다.

저장소 루트에서 다음을 실행한다.

```bash
.venv/bin/python scripts/run_synthetic_decision_demo.py \
  --cli .venv/bin/ai-work-harness
```

기본 모드는 frame/candidate/criteria digest와 approval challenge 세 값을 직접 다시
입력해야 한다. CI 전용 `--test-operator`는 이 입력을 자동 주입하지만 실제 인간 확인이나
신원 증거로 간주하지 않는다.
