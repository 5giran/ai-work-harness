# Project Story — Cofathon Harness에서 AI Decision Control Plane까지

[README로 돌아가기](../README.md)

## 출발점: 낯선 문제에서 성급하게 구현하지 않기

과제 도메인을 미리 알 수 없고 제한된 시간 안에 결과를 내야 하는 환경에서 가장 큰 위험은
AI의 코드 생성이 아니었다. 나 스스로 문제를 충분히 이해하기 전에 빠르게 구현을 시작하는 것,
그 속도 자체가 위협이 된다.

그래서 처음 만든 legacy v1은 해법을 담는 도구가 아닌 다만 순서를 강제했다.

```text
원문 캡처 → 문제 프레임 확인 → 실행 경로 선택 → 승인된 기준선 → 구현
```

입력을 먼저 보존하고, 사람이 문제 해석을 확인하고, 검증된 profile에서 결정적으로 실행
경로를 선택한 뒤에만 구현을 시작했다. 더 많은 코드를 생성하는 도구가 아니라 낯선 문제
앞에서 성급하게 틀린 코드를 만들지 않기 위한 출발 절차였다.

## 행사 후 발견한 v1의 한계

해커톤에서만 유용한 하네스라면 프로젝트의 수명도 행사 하루에 머문다. v1을 일반 AI
개발·운영 업무에 대입하자 실행 경로 선택 이후의 문제가 보였다.

- 후보가 여러 개인데 무엇을 근거로 비교했는지 남지 않는다.
- AI 추천과 사람의 최종 선택이 다르면 그 차이를 실패처럼 취급하기 쉽다.
- 입력·기준·근거가 바뀌어도 예전 승인 파일은 겉보기에 유효해 보인다.
- 모델의 초안 생성과 확인·검토·승인 권한이 섞이기 쉽다.
- `confirmed=true`만으로는 무엇을 어떤 snapshot에서 확인했는지 설명하기 어렵다.

제품의 질문을 이에 맞춰 바꿨다.

> **v1:** 어떤 과제가 와도 안전하게 구현을 시작하려면 어떻게 할까?
>
> **v2:** 사람과 AI가 어떤 근거와 권한 경계로 결정을 만들고, 변경 뒤에도 그 결정을 다시
> 검증하려면 어떻게 할까?

## v1에서 v2로 일반화한 설계

v1의 동작은 재해석하지 않고 legacy로 보존했다. v2는 명시적 `decision` namespace와 별도
schema·store·service로 구현했다.

| v1에서 발견한 한계 | v2의 구현 |
|---|---|
| 단일 profile과 route 선택 | 후보 × 기준 × 근거 × 평가의 명시적 계약 |
| boolean 중심 확인 | confirmation, review, final decision, approval을 분리한 인간 event |
| 변경 가능한 현재 파일 | JCS/SHA-256 콘텐츠 주소형 object와 불변 snapshot chain |
| 변경 후 승인의 의미가 불명확 | 의존 ref 제거와 구체적인 stale reason |
| 모델 실행과 workflow 권한이 가까움 | 저장소를 모르는 provider port와 인간 gate가 없는 MCP allowlist |
| CLI 로그 중심 검토 | digest와 schema를 다시 검증하는 read-only 오프라인 viewer |

핵심 변화는 “AI가 어떤 답을 냈는가”보다 “그 답을 어떤 근거와 권한 경계에서 만들었고,
사람이 무엇을 검토했으며, 무엇이 바뀌면 더는 유효하지 않은가”를 제품의 중심에 둔 것이다.

## 구현을 근거로 만들기

코파톤은 문제를 발견하게 한 실험 환경이다. 여기서 다루는 부분은
행사 이후 그 문제를 일반화하고 다음 판단을 코드와 실패 테스트로 닫은 과정이다.

- **문제 정의:** “AI로 빨리 만들기”를 “AI와 함께 내린 결정을 재검증하기”로 바꿨다.
- **시스템 무결성:** CAS, expected-parent, writer lock, atomic pointer와 pinned verifier로
  동시성·crash·변조를 fail-closed 처리한다.
- **권한 분리:** 모델과 MCP는 draft만 만들고 인간 confirmation·review·decision·approval은
  수행할 수 없다.
- **운영 증거:** 추천과 다른 인간 선택, upstream 변경에 따른 stale, one-way migration,
  provider 실패 시 pointer 불변을 E2E와 failure test로 재현한다.
- **제품 완결성:** CLI뿐 아니라 schema, threat model, runbook, CI와 viewer까지 제공한다.

[portfolio evidence map](evidence-map.md)은 각 주장을 실행 가능한 테스트와 연결한다. 이
프로젝트가 보여주려는 것은 대회 순위가 아니라, 불확실한 문제를 구조화하고 AI의 권한을
제한하며 판단 근거를 운영 가능한 계약으로 바꾸는 엔지니어링 과정이다.

## 안전한 프로토콜을 실제 사람이 쓰게 만들기

v2의 첫 operator interface는 저장소 계약을 정직하게 드러냈지만 일상적으로 쓰기에는 너무
복잡했다. 한 번의 결정을 진행하려면 snapshot SHA, artifact digest, challenge ID와 nonce를
계속 찾아 복사해야 했다. 이 값들은 stale write와 승인 대상 불일치를 막는 데 필요하지만,
사람이 후보·기준·위험을 이해했다는 증거는 아니었다.

v0.5.0에서는 이 문제를 안전 규칙의 완화가 아니라 presentation의 분리로 풀었다.

- raw CLI는 명시적 parent와 JSON 계약을 그대로 유지해 CI, 자동화와 forensic 운영에 쓴다.
- guided CLI는 검증된 snapshot을 process 안에 고정하고 기계용 binding을 내부 전달한다.
- 사람에게는 frame·후보·기준의 의미, 필요한 평가 review, 추천과 최종 결정의 차이, 승인
  대상을 단계별로 보여준다.
- 다른 writer가 개입하면 최신 parent에 자동 적용하지 않고 명시적 reload와 재검토를 요구한다.
- OpenAI 호출 전에는 전송 manifest를 표시하고 exact consent를 받은 뒤에만 외부 요청한다.

이 변화는 “암호값을 많이 요구하면 안전하다”가 아니라 “기계가 검증할 값과 사람이 판단할
내용을 각자에게 맞는 인터페이스로 제공해야 한다”는 실제 사용 경험에서 나온 제품 학습이다.

## 주장하지 않는 것

현재 구현은 로컬 단일 운영자용 `0.5.0` alpha다. 사용자 인증, 서명 원장, 악의적인 전체
로컬 재작성 방어, 원격 서비스, 실제 운영 성과 또는 production readiness를 주장하지
않는다. 상세한 공격 모델과 경계는 [threat model](threat-model.md)에 기록했다.
