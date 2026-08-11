# Portfolio Evidence Map

이 표는 README의 핵심 주장을 실행 가능한 test와 연결한다. 테스트 이름은 설계 의도를
설명하지만, 실제 증거는 CI에서 같은 checkout의 코드와 함께 통과했을 때 성립한다.

| 주장 | 직접 검증하는 test |
|---|---|
| 두 writer가 같은 parent로 경쟁하면 하나만 commit | [`test_two_writers_with_one_expected_parent_allow_exactly_one_commit`](../tests/test_decision_store.py) |
| snapshot write 뒤 crash가 나도 pointer는 이전 값을 유지 | [`test_failure_after_snapshot_write_leaves_pointer_old_and_snapshot_orphaned`](../tests/test_decision_store.py) |
| object/snapshot/pointer 변조는 fail-closed | [`test_object_tamper_is_detected_by_read_and_verify`](../tests/test_decision_store.py), [`test_snapshot_tamper_is_detected`](../tests/test_decision_store.py), [`test_pointer_generation_tamper_is_detected`](../tests/test_decision_store.py) |
| 동일 idempotency key 재실행은 원 결과, 다른 request는 충돌 | [`test_idempotency_replay_returns_original_snapshot_and_collision_fails`](../tests/test_decision_store.py) |
| `ready=true`인 fixture terminal snapshot은 verify 성공 | [`test_full_fixture_workflow_ready_implies_verify`](../tests/test_decision_service.py) |
| criteria 변경은 approval을 제거하고 유효한 stale graph를 생성 | [`test_upstream_change_removes_approval_and_reports_stale_reason`](../tests/test_decision_service.py) |
| approval challenge replay 금지 | [`test_approval_challenge_cannot_be_replayed_after_commit`](../tests/test_decision_service.py) |
| OpenAI refusal은 pointer를 변경하지 않음 | [`test_openai_refusal_does_not_advance_the_pointer`](../tests/test_decision_service.py) |
| fixture는 계획된 Must 실패와 `classical-ml` 추천 재현 | [`test_default_triage_fixture_reproduces_planned_failure_and_recommendation`](../tests/test_decision_providers.py) |
| strict submission tool과 bounded Responses request | [`test_openai_submission_tools_are_strict_and_draft_only`](../tests/test_decision_providers.py), [`test_openai_evaluation_request_uses_bounded_responses_defaults`](../tests/test_decision_providers.py) |
| v1 migration은 one-way이고 gate를 승격하지 않음 | [`test_complete_v1_migration_is_one_way_and_idempotent`](../tests/test_decision_migration.py) |
| MCP에 인간 gate tool이 없고 mutation은 CAS/idempotency 필수 | [`test_mcp_registry_is_an_exact_draft_read_allowlist`](../tests/test_mcp_surface.py), [`test_all_mutations_require_cas_and_idempotency`](../tests/test_mcp_surface.py) |
| viewer는 변조·중복 key를 거부하고 실제 Python export만 render | [`viewer integrity tests`](../viewer/tests/integrity.test.ts), [`Python export parity test`](../viewer/tests/example.test.ts), [`Playwright offline/tamper/accessibility E2E`](../viewer/tests/e2e/viewer.spec.ts) |

추가로 [synthetic transcript](../examples/decision-triage/TRANSCRIPT.md)는 fixture CLI E2E에서
관찰한 추천-최종 결정 불일치와 stale 결과를 기록한다. 이는 테스트 운영자의 자동 입력을
사용하므로 인증된 인간 확인 증거가 아니다.
