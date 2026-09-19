# Astra review: 검증·전송 동의·장애 복구

- 검토일: 2026-09-19
- 기준 commit: `1ff363bb2b42165605f6fc915c1bc7300e1ce184`
- 개선 브랜치: `review/astra-harness-hardening`

아래 세 결함은 기준 commit에서 재현한 원래 검토 기록이다. 후속 구현에서 semantic verifier,
공식 OpenAI endpoint 제한과 writer lock 진단을 적용했다. 이 문서의 코드 행 번호는 기준
commit을 가리킨다. 실제 외부 API 호출은 검증에 포함하지 않는다.

## 판단

인간 gate, pinned snapshot, CAS, upstream 변경에 따른 승인 무효화는 유지할 가치가 있다.
우선 보완할 부분은 모델의 추론 능력보다 **저장된 상태를 다시 믿어도 되는지**, **동의한
수신자에게 전송되는지**, **중단 후 안전하게 재개할 수 있는지**다.

## 1. P1 — verify가 최종 결정의 도메인 규칙을 재검증하지 않는다

근거: `decision/service.py:2293`의 `verify`, `:2127`의 `_verify_active_bindings`,
`:1737`의 `import_final_decision`, `:2553`의 `_status_for_snapshot`.

쓰기 시에는 선택 후보의 Must 적격성과 위험 확인을 검사하지만, 읽기 검증은 저장소
무결성과 활성 parent binding을 확인한 뒤 성공한다. 최종 결정의 후보 적격성·위험 확인을
재검사하거나 comparison을 원래 평가에서 재계산하는 단계가 없다.

### 재현

1. 기존 `tests/test_decision_service.py`의 `_build_approved_demo`로 합성 세션을 만든다.
2. 저수준 `DecisionStore.put_artifact/commit`을 통해 최종 결정 후보를 `llm-assisted`로
   바꾸고 위험 확인을 빈 배열로 저장한다. 기존 승인·challenge·bundle 참조는 제거한다.
   artifact schema와 content hash, 상위 comparison 연결은 유효하게 유지한다.
3. `service.verify`를 호출한 뒤 정상 service의 challenge 생성·승인을 진행한다.

실제 결과:

```text
eligible_candidate_ids = ['classical-ml', 'rules']
selected_candidate = 'llm-assisted'
verify.verified = true
status.ready = true
```

이는 일반 CLI import가 허용하는 우회가 아니다. 저수준 저장·복구·향후 구현 결함으로
생긴 schema-valid 상태를 verifier가 독립적으로 판정하지 못하는 문제다. 악의적인 로컬
전체 재작성 방지를 새로 요구하는 것도 아니다. 현재 문서의 “full workflow policy 검증”과
실제 검증 범위 사이에 차이가 있다.

### 개선 및 완료 조건

- 쓰기와 읽기에서 공유하는 순수 도메인 검증 함수를 둔다. 읽기 검증이 mutation 함수를
  호출해 새 이벤트나 snapshot을 만들지 않도록 한다.
- pinned snapshot의 gate 존재, 평가 matrix·근거 binding, review 완료, comparison 재계산,
  최종 후보·위험 확인·추천 관계를 검증한다. 부분 진행 상태는 해당 단계까지만 검사한다.
- 승인 생성·commit과 readiness/export가 같은 semantic verifier를 사용하도록 한다.
- 위 잘못된 최종 결정이 verify와 승인 단계에서 거부되고, `ready=true`로 노출되지 않는
  회귀 테스트를 추가한다. 정상 select/reject_all과 진행 중 세션은 계속 통과해야 한다.

## 2. P1 — 외부 전송 동의에 실제 API 목적지가 포함되지 않는다

근거: `decision/service.py:854`의 outbound manifest, `:1305`의 provider configuration,
`decision/openai_provider.py:534`의 `_get_client`.

manifest는 provider/model/input/limits를 고정하지만 endpoint를 포함하지 않는다. 실제
client는 `OpenAI(timeout=..., max_retries=0)`로 생성되며 SDK의 endpoint 환경 설정을
그대로 사용한다. 따라서 model과 input이 같아도 동의 시점과 실행 시점의 수신자가 달라질
수 있다. 다른 작업을 위해 남겨 둔 endpoint 설정도 이 문제를 일으킬 수 있다.

### 재현

임시 Python process에서 합성 API key와 `OPENAI_BASE_URL=https://recipient.invalid/v1`을
설정하고 client를 생성했다. 네트워크 요청은 하지 않았다. 설치된 OpenAI SDK 2.54.0의
생성자 소스에서도 해당 환경변수 사용을 확인했다.

```text
actual client.base_url = https://recipient.invalid/v1/
consent/provider configuration = model, effort, limits ... (endpoint 없음)
```

### 개선 및 완료 조건

- 현재 OpenAI 전용 범위에서는 공식 endpoint를 명시적으로 고정하고, 지원하지 않는
  endpoint override가 있으면 전송 전에 명확한 오류를 내는 방법을 우선 검토한다.
- custom endpoint를 지원하려면 정규화된 endpoint를 manifest·preview·consent·run trace에
  포함하고 실제 client와 일치하는지 호출 전에 확인한다. SDK의 다른 endpoint 설정 경로와
  injected client도 함께 검토한다.
- preview 이후 endpoint를 바꾸면 network call 0회로 거부되는 contract test를 추가한다.
- custom endpoint 지원을 선택하면 이전 consent와의 schema/호환성 정책도 함께 정한다.

## 3. P2 — 비정상 종료 후 남는 writer lock을 doctor가 진단하지 않는다

근거: `decision/store.py:908`의 `_writer_lock`, `:456`의 `doctor`,
`docs/operator-runbook.md`의 `WRITE_LOCK_TIMEOUT` 설명.

lock은 exclusive-create 파일이며 정상 종료 시 `finally`에서 삭제된다. process가 강제
종료되면 파일은 남는다. 이후 쓰기는 계속 timeout이 나지만 doctor는 lock을 검사하지
않는다. runbook에는 다른 writer를 확인하라는 안내만 있고, 확인된 잔여 lock을 안전하게
복구하는 구체적인 절차가 없다.

### 재현

임시 저장소를 초기화하고 자식 process가 lock을 획득한 직후 `os._exit(0)`으로 종료하게
했다. 이는 정상 cleanup이 실행되지 않는 종료를 재현한다.

```text
writer_lock_exists = true
doctor.ok = true
doctor.issues = []
next write = WRITE_LOCK_TIMEOUT
```

### 개선 및 완료 조건

- doctor가 lock 존재·metadata·소유 process 관찰 결과를 별도 진단하도록 한다. 무결성 정상과
  쓰기 가능 상태를 구분한다. 소유자를 확인할 수 없으면 unknown으로 보고한다.
- 자동 해제를 제공하려면 PID 재사용과 동시 writer 경쟁을 다룰 수 있는 소유권 설계가
  먼저 필요하다. 나이 또는 PID 존재 여부만으로 lock 파일을 자동 삭제하지 않는다.
- 우선 운영자가 모든 writer 중지를 확인하고 복구할 수 있는 절차를 runbook에 추가한다.
- subprocess 비정상 종료, 활성 writer 유지, 잘못된 metadata, 복구 후 재시도에 대한
  테스트를 추가한다. Windows 동작도 검증한다.

## 구현 결과

1. `decision/verification.py`와 공유 domain validator로 단계별 정책을 다시 검사하고
   approval/readiness/export와 쓰기 전 parent 검증에 연결했다.
2. `https://api.openai.com/v1` 고정 정책을 선택했다. manifest schema를 변경하지 않고 환경과
   실제 client의 주소를 매 요청·retry 전에 확인한다. 과거 run의 endpoint는 소급 보증하지 않는다.
3. doctor에 별도 lock 진단을 추가했다. Windows는 위험한 PID signal probe 없이 unknown으로
   보고하며 자동 lock 삭제는 하지 않는다. runbook에 독립적인 writer 종료 확인과 수동 백업
   절차를 문서화하고 기존 cross-platform clean-wheel smoke에 crash 복구 검증을 추가했다.

기존 artifact는 재작성하지 않는다. 진행 중 세션과 과거의 정상 snapshot은 계속 검증한다.
변경 후 회귀 검증 근거는 [Evidence map](evidence-map.md)에 연결했다.

## 검증 기록

- 세 재현은 임시 디렉터리와 합성 데이터로 실행했다. 실제 사용자 의사결정 저장소를
  변경하거나 실제 API에 데이터를 전송하지 않았다.
- Python 3.12.0에서 기존 Python suite 365개가 모두 통과했다(exit 0).
  실행 명령은 `pytest -q -p no:cacheprovider`이며 임시 가상환경에 dev/openai/mcp
  의존성을 설치했다. coverage 측정이나 viewer suite는 이번 실행에 포함하지 않았다.
- 기존 테스트 통과와 별개로 위 세 경계 사례는 재현됐다. 후속 구현에는 semantic 20개,
  endpoint 14개, writer lock 12개의 회귀 테스트를 추가했다. 변경 후 전체 검증 결과는 PR에 기록한다.
