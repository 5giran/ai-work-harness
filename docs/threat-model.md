# Threat Model

## 보호하려는 것

- 승인 대상과 실제 현재 artifact가 의도치 않게 달라지는 문제
- 충돌하는 두 writer가 서로의 update를 덮어쓰는 문제
- 부분 쓰기와 crash가 완성되지 않은 snapshot을 current로 만드는 문제
- source line 또는 evidence binding이 바뀌었는데 과거 평가·승인을 그대로 쓰는 문제
- AI/provider/MCP가 인간 gate를 호출하거나 저장소를 직접 변경하는 문제
- viewer bundle 변조 뒤 검증되지 않은 결정을 정상 화면으로 보여주는 문제

## 신뢰 경계

로컬 운영자와 filesystem은 운영 환경의 신뢰 기반이다. core는 일반적인 실수, drift,
동시 write, 손상과 제한된 untrusted import/provider output을 방어한다. provider에는 frozen,
검증된 DTO만 전달하고 저장소 handle을 주지 않는다. MCP annotation은 권한이 아니며 등록된
closed-schema allowlist가 실제 경계다.

## 통제

| 위협 | 통제 | 남는 위험 |
|---|---|---|
| stale write | full expected-parent CAS | 운영자가 올바른 current digest를 다시 읽어야 함 |
| partial/crash write | fsync + atomic replace; immutable objects | orphan object가 남을 수 있음 |
| artifact drift | JCS SHA-256, parent binding, pinned verifier | 전체 tree를 다시 쓸 수 있는 공격자는 새 digest도 만들 수 있음 |
| path traversal/symlink | safe ID와 regular-file 검증 | 신뢰된 운영자가 별도 도구로 filesystem을 변경할 수 있음 |
| 근거 위조 | source locator byte hash와 provenance 분리 | source 내용 자체의 진실성은 별도 검증 대상 |
| model overreach | draft-only provider port, no repository handle | prompt injection이 부정확한 draft를 만들 수 있어 인간 review 필요 |
| MCP overreach | exact tool allowlist; human gate tool 미등록 | stdio client와 host의 보안은 본 프로젝트 밖 |
| approval replay | current-parent, bundle, challenge ID, nonce, expiry binding | actor identity는 인증하지 않음 |
| viewer tampering | schema + artifact digest + top-level integrity fail-closed | 신뢰된 원본 bundle 배포 경로를 제공하지 않음 |

## 명시적으로 주장하지 않는 것

- `actor_label=local_operator`는 인증된 사용자 계정이 아니다.
- `identity_verified=false`이며 approval은 신원 확인 증거가 아니다.
- SHA-256/JCS는 tamper-evident한 외부 원장이나 디지털 서명이 아니다.
- 로컬 secret 보관, sandbox, OS 권한 관리, 악성 dependency 방어를 제공하지 않는다.
- remote transport, multi-tenant 격리, 서버 인증·인가, availability SLA가 없다.
- AI 결과의 사실성·공정성·법규 준수를 자동 보장하지 않는다.

## 운영상 권고

실제 민감 데이터를 Phase 1 demo나 live provider에 보내지 않는다. 외부 provider preview에서
provider/model/prompt/input digest와 source byte count를 확인하고 consent 후에도 전송 범위를
별도로 검토한다. `decision doctor`와 pinned `decision verify --snapshot <sha>`를 approval 전후에
실행하고, export bundle은 원 source의 대체물이 아니라 검토용 sanitized view로 다룬다.
