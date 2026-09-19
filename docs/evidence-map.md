# Portfolio Evidence Map

이 문서는 프로젝트의 핵심 주장을 같은 checkout에서 실행되는 자동화 검증과 연결한다.
테스트 이름은 설계 의도를 설명할 뿐이며, 직접 증거는 해당 코드와 테스트가 CI에서 함께
통과했을 때 성립한다.

## 직접 검증되는 보장

### 불변 저장소와 결정 정책

| 주장 | 직접 검증하는 test |
|---|---|
| 두 writer가 같은 parent로 경쟁하면 하나만 commit | [`test_two_writers_with_one_expected_parent_allow_exactly_one_commit`](../tests/test_decision_store.py) |
| snapshot write 뒤 crash가 나도 pointer는 이전 값을 유지 | [`test_failure_after_snapshot_write_leaves_pointer_old_and_snapshot_orphaned`](../tests/test_decision_store.py) |
| object·snapshot·pointer 변조는 fail-closed | [`test_object_tamper_is_detected_by_read_and_verify`](../tests/test_decision_store.py), [`test_snapshot_tamper_is_detected`](../tests/test_decision_store.py), [`test_pointer_generation_tamper_is_detected`](../tests/test_decision_store.py) |
| 동일 idempotency key 재실행은 원 결과를 반환하고 다른 request는 충돌 | [`test_idempotency_replay_returns_original_snapshot_and_collision_fails`](../tests/test_decision_store.py) |
| `ready=true`인 fixture terminal snapshot은 같은 snapshot에서 verify 성공 | [`test_full_fixture_workflow_ready_implies_verify`](../tests/test_decision_service.py) |
| criteria 변경은 approval을 제거하고 검증 가능한 stale graph를 생성 | [`test_upstream_change_removes_approval_and_reports_stale_reason`](../tests/test_decision_service.py) |
| approval challenge는 replay할 수 없고 동일 final에 active approval이 있으면 다시 발급할 수 없음 | [`test_approval_challenge_cannot_be_replayed_after_commit`](../tests/test_decision_service.py), [`test_same_final_and_active_approval_cannot_be_rechallenged`](../tests/test_decision_service.py) |
| `request_revision`은 비교를 막고 같은 review 덮어쓰기가 아닌 새 evaluation을 요구 | [`test_revision_request_is_recorded_and_requires_a_new_evaluation`](../tests/test_decision_service.py), [`test_request_revision_commits_only_the_reviewed_prefix_and_returns_to_drafting`](../tests/test_guided_workflow.py) |

추가 경계 회귀 검증:

- [Semantic verification tests](../tests/test_decision_semantic_verification.py): schema/hash가
  유효해도 필수 gate 누락, 잘못된 근거·평가·review·comparison·최종 결정을 거부하며
  승인·readiness·export를 차단한다. 정상 진행 중 snapshot은 통과한다.
- [Writer lock diagnostics tests](../tests/test_writer_lock_diagnostics.py): subprocess 강제 종료,
  활성 writer, 잘못된 metadata, Windows의 unknown 처리, 파일 변경과 수동 복구를 검사한다.
  [clean-wheel smoke](../scripts/ci_smoke.py)는 기존 Linux/macOS/Windows CI에서 종료 후 lock
  진단과 writer 종료 확인 뒤 수동 백업·쓰기 재개를 실행한다.

### Guided operator UX

| 주장 | 직접 검증하는 test |
|---|---|
| planner는 단계별 행동을 결정적으로 계산하고 public plan에서 challenge 비밀을 제거 | [`test_planner_progression_table`](../tests/test_operator_planner.py), [`test_challenge_boundary_is_explicit_and_public_value_is_sanitized`](../tests/test_operator_planner.py) |
| operator plan 조회는 쓰지 않고 한 pinned snapshot만 읽으며 무결성 오류를 숨기지 않음 | [`test_operator_plan_is_read_only_and_uses_the_raw_next_envelope`](../tests/test_operator_read_service.py), [`test_read_operator_state_remains_pinned_when_current_moves`](../tests/test_operator_read_service.py), [`test_operator_plan_fails_closed_on_integrity_error`](../tests/test_operator_read_service.py) |
| guided import와 semantic confirmation은 별도 snapshot이고 raw confirmation 방식은 그대로 유지 | [`test_guided_import_and_semantic_confirm_are_separate_snapshots`](../tests/test_guided_operator_cursor.py), [`test_guided_confirmation_method_is_additive_and_raw_default_is_unchanged`](../tests/test_decision_service.py) |
| guide는 challenge와 approval을 별도 snapshot으로 기록하면서 nonce·full binding을 화면 밖에서 전달 | [`test_guided_challenge_and_commit_use_separate_secret_bound_snapshots`](../tests/test_guided_operator_cursor.py) |
| mutation cursor는 성공 응답에서만 전진하고 충돌 뒤 명시적 reload 전에는 자동 재시도하지 않음 | [`test_cursor_advances_only_after_successful_mutation`](../tests/test_guided_operator_cursor.py), [`test_write_conflict_preserves_pinned_cursor_until_explicit_reload`](../tests/test_guided_operator_cursor.py), [`test_write_conflict_decline_does_not_reload_or_retry_guided_mutation`](../tests/test_guided_workflow.py), [`test_write_conflict_reload_is_explicit_and_does_not_retry_mutation`](../tests/test_guided_workflow.py) |
| non-TTY는 session 생성 전에 실패하고 confirmation 취소는 draft snapshot을 보존 | [`test_non_tty_fails_before_session_creation`](../tests/test_guided_workflow.py), [`test_json_draft_survives_confirmation_quit_without_confirmation_snapshot`](../tests/test_guided_workflow.py) |
| 한 controller invocation 안에서 인간 gate를 거쳐 approval·current export·`ready=true`에 도달 | [`test_fixture_workflow_reaches_approved_ready_in_one_controller_invocation`](../tests/test_guided_workflow.py) |
| 같은 payload의 raw와 guided workflow는 동일한 domain payload와 동등한 active ref graph를 생성 | [`test_raw_and_guided_fixture_workflows_have_equivalent_semantic_graphs`](../tests/test_guided_equivalence.py) |

### Provider, MCP, migration과 viewer

| 주장 | 직접 검증하는 test |
|---|---|
| exact OpenAI 문구가 틀리면 consent snapshot과 외부 요청이 모두 생기지 않음 | [`test_openai_exact_phrase_mismatch_creates_no_consent_or_network_call`](../tests/test_guided_workflow.py) |
| guided OpenAI consent와 결과는 별도 snapshot이며 full manifest binding은 public plan에 노출되지 않음 | [`test_guided_openai_evaluation_uses_separate_consent_and_result_snapshots`](../tests/test_guided_workflow.py), [`test_guided_outbound_preview_and_exact_consent_keep_full_binding_internal`](../tests/test_guided_operator_cursor.py) |
| provider 실패 뒤 동일 active consent로만 재시도하고 recommendation 뒤에도 인간 final gate로 돌아감 | [`test_failed_guided_openai_run_reuses_active_consent_without_new_preview`](../tests/test_guided_workflow.py), [`test_guided_openai_recommendation_returns_to_human_final_decision`](../tests/test_guided_workflow.py) |
| OpenAI refusal은 pointer를 변경하지 않음 | [`test_openai_refusal_does_not_advance_the_pointer`](../tests/test_decision_service.py) |
| fixture는 계획된 Must 실패와 `classical-ml` 추천을 재현 | [`test_default_triage_fixture_reproduces_planned_failure_and_recommendation`](../tests/test_decision_providers.py) |
| OpenAI submission tool은 strict draft-only이고 Responses request 한도는 고정 | [`test_openai_submission_tools_are_strict_and_draft_only`](../tests/test_decision_providers.py), [`test_openai_evaluation_request_uses_bounded_responses_defaults`](../tests/test_decision_providers.py) |
| v1 migration은 one-way이고 과거 gate를 v2 gate로 승격하지 않음 | [`test_complete_v1_migration_is_one_way_and_idempotent`](../tests/test_decision_migration.py) |
| MCP에 인간 gate tool이 없고 mutation은 CAS와 idempotency가 필수 | [`test_mcp_registry_is_an_exact_draft_read_allowlist`](../tests/test_mcp_surface.py), [`test_all_mutations_require_cas_and_idempotency`](../tests/test_mcp_surface.py) |
| viewer는 변조·중복 key를 거부하고 Python이 export한 bundle만 render | [`viewer integrity tests`](../viewer/tests/integrity.test.ts), [`Python export parity test`](../viewer/tests/example.test.ts), [`Playwright offline/tamper/accessibility E2E`](../viewer/tests/e2e/viewer.spec.ts) |

[OpenAI endpoint policy tests](../tests/test_openai_endpoint_policy.py)는 환경 설정과
injected/factory/cached client의 다른 주소를 거부하고, 동의 뒤 변경과 retry 중 변경도
추가 network call 없이 차단하는지 검증한다. 실제 외부 API 호출은 사용하지 않는다.

## 관찰 가능한 데모 자료

[한국어 guided transcript](../examples/decision-triage/TRANSCRIPT.md)와
[English guided transcript](../examples/decision-triage/TRANSCRIPT.en.md)는 CI에서 실행하는
[합성 fixture 데모](../scripts/run_synthetic_decision_demo.py)의 긴 터미널 출력을 핵심 인간
checkpoint만 남겨 정리한 관찰 자료다. 두 transcript는 다음 흐름을 보여준다.

- 하나의 guide invocation
- Must·High 평가 cell 9개 검토
- fixture 추천 `classical-ml`과 최종 선택 `rules`의 정상적인 불일치
- 사람이 의미를 확인한 exact approval phrase
- 승인된 current snapshot export
- 같은 invocation에서 criteria 변경 뒤 `ready=false`와 stale 사유 기록

CI의 `--test-operator`는 이 흐름을 재현 가능하게 만드는 결정적 입력 장치다. 실제 사람이나
신원을 인증하지 않으며, transcript도 인간 승인 또는 신원 증거로 사용하지 않는다.
