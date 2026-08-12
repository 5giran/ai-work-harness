# Provider Policy

## Port

core가 공개하는 두 provider 역할은 같은 immutable `FrozenDecisionContext` DTO type을
입력으로 받는다. 다만 내용은 operation별로 최소화된다. evaluation context는 이전
evaluation과 comparison을 제외하고, recommendation context는 검토된 evaluation과
결정적 comparison을 유지한다. recommendation은 comparison gate를 통과한 뒤에만 호출된다.

```text
EvaluationProvider.generate(FrozenDecisionContext) -> EvaluationDraft
RecommendationProvider.generate(FrozenDecisionContext) -> RecommendationDraft
```

provider에는 repository/store handle, confirmation/review/final/approval callback을 주지 않는다.
반환값은 closed payload schema와 현재 candidate/criterion/evidence binding을 통과한 뒤에만
core가 새 snapshot으로 commit한다. 실패한 provider 호출은 pointer를 바꾸지 않는다.
v0.5의 raw CLI와 guided TTY는 이 port와 service를 함께 사용하며, guided layer가 provider에
추가 권한이나 저장소 접근을 주지 않는다.

## Fixture

`FixtureProvider`는 `examples/decision-triage`의 명시적 candidate/criterion/evidence ID에 대해
결정적 payload를 만든다. producer는 `fixture`이며 model 실행을 주장하지 않는다. PR CI와
기본 데모는 fixture만 사용한다.

## OpenAI adapter

선택적 adapter는 코드상 다음 bounded request policy를 가진다.

- Responses API와 strict 제출 function schema
- 기본 model `gpt-5.6-luna`, reasoning effort `medium`, `store=false`
- 버전화 prompt `evaluation-v1`, `recommendation-v1`과 prompt SHA-256
- 전체 frozen context 1,000,000 JCS bytes, 누적 lookup output 1,000,000 bytes,
  outbound source excerpt 100,000 UTF-8 bytes, evidence 50개
- tool round 12, output 8,000 tokens, timeout 60초 상한
- 429, 5xx, network error만 최대 2회 재시도
- refusal, malformed output, tool limit, credential/model unavailable에는 자동 fallback 없음

환경변수 model override는 outbound manifest에 새 model 값으로 들어가므로 이전 consent를
재사용할 수 없다. 성공한 실행만 agent-run binding을 기록한다. validate replay는 외부 호출
없이 기록된 input/output schema와 digest를 다시 검사한다.

### Exact consent: raw와 guided

live 실행 전 read-only preview는 provider/model/operation, prompt와 input digest, 입력 snapshot,
source digest, evidence·excerpt 크기와 모든 실행 한도를 포함한 closed outbound manifest와 full
manifest SHA-256을 계산한다. raw protocol은 그 full digest를
`agent consent --expected-manifest-sha`로 제출하고 `method=digest_challenge`를 기록한다.

guide는 raw source, excerpt 본문, source digest 전체 목록과 full manifest digest를 출력하지
않는다. 대신 operation/provider/model, prompt ID와 prompt/input fingerprint, excerpt byte 수,
evidence 수와 12자 manifest fingerprint를 보여주고 다음 정확 문구를 요구한다.

```text
SEND OPENAI <12자 manifest fingerprint>
```

문구가 일치하면 `GuidedOperator`가 사용자가 보던 pinned preview의 full digest를 내부 전달하고
`method=guided_exact_phrase`인 별도 consent snapshot을 만든다. service는 네트워크 호출 전에
같은 `expected_parent`에서 manifest를 다시 계산해 full digest가 정확히 일치하는지 검증한다.
따라서 12자 문구는 사용자 확인 표현이고, 저장·실행 binding은 full SHA-256이다.

consent는 manifest의 operation, provider/model, prompt/input/source digest, 모든 한도와 입력
snapshot을 함께 binding하며, 그 consent를 `agent_consent` active ref로 가진 정확한 current
snapshot에서만 실행할 수 있다. pointer를 바꾸는 upstream mutation이나 provider 설정 변경
뒤에는 새 preview와 consent가 필요하다. 한 consent는 성공한 result와 agent-run commit 한
번에만 사용되고 그 commit에서 active consent ref가 제거된다.

### Active-consent retry

timeout, refusal 또는 transport/model 실패가 result commit 전에 끝나면 pointer는 consent
snapshot에 머물고 `agent_consent`도 소비되지 않는다. guided planner는 이 상태를 일반
evaluation/recommendation 단계보다 먼저 인식해 같은 operation의 retry 또는 local JSON/MCP·agent
import를 제시한다. retry는 반드시 운영자가 다시 선택하며 preview나 consent snapshot을 새로
만들지 않는다. 저장된 input snapshot과 manifest로 context와 provider 설정을 복원하고 active
consent/full manifest binding을 다시 검증한 뒤 같은 요청만 실행한다.

성공하면 result와 agent-run을 한 snapshot에 commit하고 consent ref를 제거한다. local 또는
agent import를 선택하면 해당 result mutation의 stale graph가 기존 consent/run ref를
무효화한다. guide가 실패한 호출을 자동 재개하거나 다른 provider/fixture로 fallback하지는
않는다. 다만 한 번의 명시적 OpenAI run 내부에서는 위 bounded policy대로 429, 5xx와 network
error만 최대 2회 자동 재시도한다.

공식 구현 근거는 [OpenAI model guide](https://developers.openai.com/api/docs/guides/latest-model),
[GPT-5.6 Luna guidance](https://developers.openai.com/api/docs/guides/model-guidance?model=gpt-5.6-luna),
[Responses create API](https://developers.openai.com/api/reference/resources/responses/methods/create),
[strict function calling](https://developers.openai.com/api/docs/guides/function-calling#strict-mode)이다.
문서와 SDK가 바뀌면 이 저장소의 adapter contract test와 prompt digest를 함께 갱신한다.

## MCP

`ai-work-harness-mcp`는 선택적인 공식 Python SDK stdio server다. remote HTTP transport와
인증은 범위 밖이다. 모든 request는 explicit session ID를 사용하며 mutation에는 full
expected parent와 idempotency key를 요구한다.

공개 allowlist는 session/status/source 조회, frame/candidate/criteria/evaluation draft,
deterministic comparison, recommendation record, verify로 제한한다. 이름이나 schema에
confirm, review, final, challenge, approval은 없다. tool annotation은 host UI hint이며 보안
권한은 아니다. 상태를 진행하거나 하위 ref를 무효화할 수 있는 MCP mutation에는
`destructiveHint=true`가 표시되지만, 이 값 자체는 authorization이나 안전 경계를 제공하지
않는다. 실제 경계는 server registry allowlist, closed input schema와 core policy 검증이다. 참고:
[MCP tool contract](https://modelcontextprotocol.io/specification/2025-06-18/server/tools),
[tool annotation security guidance](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/).

## CI와 credential

PR/push CI는 fixture, injected fake client 또는 recorded shape만 사용하며 live OpenAI request를
만들지 않는다. credential이 없는 상태를 정상적인 test 조건으로 다룬다. live smoke가
필요하면 `manual-openai-live-smoke` workflow를 사람이 dispatch한다. 이 job은
`openai-live-smoke` GitHub environment와 그 environment의 `OPENAI_API_KEY`를 요구하고,
synthetic source만 사용해 consent-bound evaluation draft 한 번을 생성한 뒤 replay validation을
실행한다. 저장소 관리자는 실행 전 해당 environment에 required reviewer를 구성해야 한다.
이 workflow는 기본 CI 합격 조건이 아니다.
