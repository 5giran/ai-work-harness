# ai-work-harness

[한국어](README.md) | [English](README.en.md)

사람의 명시적 승인을 거치는 Python 기반 AI 빌드 하네스 프로토타입

`ai-work-harness`는 빌드 결정을 승인하기 전에 다음과 같은 로컬 산출물의 흐름을
기록합니다.

1. 로컬 입력을 관리 저장소로 복사하고 콘텐츠 해시를 기록합니다.
2. 사용자의 원문과 AI의 해석을 분리해 보관합니다.
3. 합의된 작업 정의(frame)에 사용자의 명시적 확인을 요구합니다.
4. 제한된 레지스트리에서 하나의 결정적 경로(route)를 선택합니다.
5. 입력, frame, profile, route, 결정과 사유를 하나의 승인 기록으로 묶습니다.

이전 승인 기록은 삭제하지 않고 보존합니다. `status`는 현재 파일을 기준으로 각
승인의 유효성을 다시 계산하고, 승인이 현재 산출물과 일치하지 않는 stale 상태라면
그 이유를 알려 줍니다.

## 범위와 한계

이 저장소는 로컬 산출물 기록, 결정적 경로 선택(routing), 승인 정보 연결(binding)
흐름만 구현합니다.
모델 호출, 생성된 작업의 실행, 사용자 신원 확인, 변경 불가능한 승인 기록,
원격 서비스 연동은 구현 범위에 포함하지 않습니다. 해시는 일반적인 로컬 변경을
감지하지만, 모든 산출물을 다시 쓸 수 있는 사용자는 해시도 다시 계산할 수 있습니다.

현재 등록된 route는 두 개뿐입니다.

| 입력 소스 | 로컬 modality | capability 묶음 | Route |
|---|---|---|---|
| `local_files` | `text`, `json`, `csv` 중 하나 이상의 조합 | `transform`, `aggregate`, `rank` 중 하나 이상의 조합 | `batch_pipeline` |
| `local_files` | `text`, `json`, `csv` 중 하나 이상의 조합 | `validate`, `apply_rules` 중 하나 이상의 조합 | `rule_decision` |

네트워크, live API, retrieval, multimodal, 미등록 경로, 서로 다른 capability 묶음을
혼합한 경로는 구조화된 오류와 0이 아닌 종료 코드를 반환하며 안전하게 거부됩니다.

## 설치

Python 3.11과 3.12를 지원합니다.

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ai-work-harness --version
```

## 합성 데이터로 실행해 보기

저장소의 `examples/synthetic/`에는 외부 자료를 사용하지 않고 임의로 만든 작은 CSV가
포함되어 있습니다. 아래 명령은 초기화부터 입력 캡처, 작업 정의, route 선택, 승인,
상태 확인과 검증까지 전체 흐름을 실행합니다.

```bash
HARNESS=".venv/bin/ai-work-harness"
DEMO_ROOT="$(mktemp -d)"

# 작업 디렉터리 초기화
"$HARNESS" --root "$DEMO_ROOT" init

# 로컬 입력을 관리 저장소로 복사하고 해시 기록
"$HARNESS" --root "$DEMO_ROOT" capture \
  --id records.csv \
  --file examples/synthetic/records.csv

# 사용자 요청, AI 해석과 사용자가 확인한 작업 정의 기록
"$HARNESS" --root "$DEMO_ROOT" frame \
  --user-statement "Group the local records into a reviewable output." \
  --ai-interpretation "A deterministic batch transformation is sufficient." \
  --confirmed \
  --agreed-frame "Produce a local batch artifact without external access."

# 입력 특성과 필요한 기능에 맞는 등록 경로 선택
"$HARNESS" --root "$DEMO_ROOT" route \
  --input-source local_files \
  --modality csv \
  --capability transform \
  --capability aggregate \
  --objective "Create a deterministic grouped artifact."

# 선택한 경로를 사유와 함께 승인
"$HARNESS" --root "$DEMO_ROOT" approve \
  --decision "Use the batch pipeline route." \
  --reason "The confirmed frame and local input match the registered route."

# 현재 승인 상태 확인과 전체 산출물 검증
"$HARNESS" --root "$DEMO_ROOT" status
"$HARNESS" --root "$DEMO_ROOT" verify
```

모든 명령 결과는 JSON으로 출력됩니다. 정상적으로 예상된 workflow 실패도 JSON으로
표현되어 stderr에 기록되며 종료 코드 `2`를 반환합니다.

## 로컬 산출물

`init`을 실행하면 Git이 추적하지 않는 `.ai-work-harness/` 디렉터리가 생성됩니다.

```text
.ai-work-harness/
├── input-manifest.json
├── inputs/
├── frame.json
├── task-profile.json
├── route.json
└── approvals/
```

입력 매니페스트(manifest)의 각 항목에는 다음 정보만 포함됩니다.

- 안전한 상대 경로 형식의 `logical_id`
- 바이트 수
- SHA-256 해시값

매니페스트는 원본 경로, 시각 정보, 콘텐츠 미리 보기, 환경 메타데이터를 저장하지
않습니다. 관리되는 입력 경로는 검증된 logical ID로부터 만들어집니다.

승인 기록은 현재 입력 manifest, frame, task profile, route, 결정과 사유를 하나로
묶습니다. 나중에 `capture`를 다시 실행하거나 frame/profile/route를 변경해도 이전
기록은 남지만, 해당 승인은 stale 상태가 됩니다.

## 스키마와 결정적 출력

버전이 지정된 JSON Schema는 `src/ai_work_harness/schemas/v1/`에 포함되어 있습니다.
실행 중 기록되는 데이터는 JSON Schema Draft 2020-12로 검증하며, 모든 객체는 선언되지
않은 필드를 거부합니다. 저장되는 JSON은 UTF-8, 정렬된 키, 공백 없는 구분자와
마지막 줄바꿈을 사용합니다. route 산출물에는 시각에서 파생된 값이 없으므로, 정규화된
입력이 같으면 항상 동일한 바이트가 생성됩니다.

## 개발 검증

```bash
ruff check .
ruff format --check .
pytest
python -m pip wheel . --no-deps --wheel-dir dist
```

CI matrix는 깨끗한 checkout에서 Python 3.11과 3.12 각각에 대해 lint와 test를
실행합니다.

## 라이선스

MIT
