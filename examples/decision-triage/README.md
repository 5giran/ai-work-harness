# 합성 고객문의 triage 결정

외부 자료나 실제 고객 데이터를 사용하지 않고 Guided Operator UX 전체 흐름을 보여주는
fixture 시나리오다. 세 후보 `rules`, `classical-ml`, `llm-assisted`를 다음 기준으로
비교한다.

| 우선순위 | 기준 |
|---|---|
| Must | privacy, auditability |
| High | classification-quality |
| Medium | latency, operating-cost |

결정적 fixture는 `classical-ml`을 추천한다. 운영자는 비교 결과와 필수 위험을 직접 확인한
뒤 `rules`를 선택하며, 하네스는 이를 오류가 아닌 `recommendation_relation=different`로
기록한다. `llm-assisted`는 privacy Must를 통과하지 못해 선택 후보가 될 수 없다.

## 직접 실행

저장소 루트에서 한국어 guide를 실행한다.

```bash
.venv/bin/python scripts/run_synthetic_decision_demo.py --lang ko
```

영어 화면은 다음과 같다.

```bash
.venv/bin/python scripts/run_synthetic_decision_demo.py --lang en
```

스크립트는 임시 root와 합성 evidence payload를 준비한 뒤
`ai-work-harness decision guide`와 같은 controller를 정확히 한 번 호출한다. frame·후보·기준
확인, Must·High cell 검토, 최종 결정, exact approval은 사람이 입력해야 하지만 snapshot
SHA, artifact digest, challenge ID와 nonce는 복사하지 않는다. 승인 뒤 선택한 경로에 current
viewer JSON을 내보낼 수 있고, 같은 guide 안에서 criteria를 바꾸면 downstream stale 항목을
미리 보여준다.

## 재현 가능한 CI 시나리오

결정적인 전체 실행은 다음 명령으로 재현한다.

```bash
.venv/bin/python scripts/run_synthetic_decision_demo.py \
  --test-operator \
  --lang ko
```

`--test-operator`는 다음 순서를 자동 입력한다.

1. frame·후보·기준의 semantic summary를 확인한다.
2. 세 후보의 Must·High cell 9개를 검토한다.
3. fixture 추천 `classical-ml`을 확인하고 최종적으로 `rules`를 선택한다.
4. `rules`에 남은 High·Medium 위험을 확인한다.
5. 화면에 표시된 `APPROVE rules <fingerprint>` 문구와 승인 이유를 입력한다.
6. 승인된 current snapshot을 viewer JSON으로 export한다.
7. 같은 guide invocation에서 `criteria.changed.json`을 import해 기존 결정을 stale로 만든다.

전체 digest, UUID, nonce와 timestamp는 실행마다 달라질 수 있어 transcript에서는 생략한다.
자동 test operator는 CI 입력일 뿐 실제 인간 확인, 인간 신원 또는 인증된 승인 증거가 아니다.

- [한국어 curated transcript](./TRANSCRIPT.md)
- [English curated transcript](./TRANSCRIPT.en.md)
