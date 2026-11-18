# Wing-C-Assignment — 과제 명세·과제 생성 (P1)

> 레포 `JangYunSeong/Wing-C-Assignment` (private, 옛 이름 `p1-task-spec`). 파이썬 패키지 이름은 그대로 `task-spec`(`src/task_spec`)입니다.

이력서 + 채용 공고 + 직무 역량 온톨로지를 받아 **과제 명세(task_spec)** 초안을 만들고,
**사람이 승인해야만** 확정하는 서비스입니다. 승인된 기능 개발형 공통 과제는 **시작 코드·숨은 테스트·응시용/채점용 컨테이너 이미지**까지 만들고,
다시 사람 승인을 거쳐 응시 플랫폼에 넘깁니다. FastAPI(인터페이스) 위에서 LangGraph 파이프라인이 돌고, 과제 생성은 따로 띄운 builder 가 맡습니다.

- 명세 스키마의 기준: 이 레포 `docs/design/task-spec-schema.md` (2026-11-11 온톨로지 레포 `context.md` §6 에서 옮김)
- 고영향 AI(AI 기본법)로 다루므로 최종 판단은 사람이 합니다. 명세 승인과 빌드 승인 모두 LangGraph `interrupt()` 로만 받고, 승인 없이 확정되는 경로는 없습니다.
- 규칙·불변식 전체는 [`CLAUDE.md`](CLAUDE.md) 에 있습니다. 이 README 는 개요와 실행 방법만 다룹니다.

## 전체 흐름

```
공고 + 이력서 → 명세 그래프 (POST /runs) → 명세 승인 (interrupt)
                                              │ 승인된 기능형 공통 과제
                                              ▼
              과제 생성 요청 (POST /common-tasks/{common_id}/builds) — 큐에 넣기만
                                              ▼
              builder: 계약 → 시작 코드·테스트 생성·검증 → 응시용·채점용 이미지 → 묶음 등록
                                              ▼
              빌드 승인 (interrupt) → 묶음 확정·평가 키트 → 전달 (SFTP 로 보내기 또는 /pull 로 가져가기)
```

## 명세 파이프라인

```
START → posting_profile ─┬─ (문서 있음) ingest → pseudonymize ─┬─ (통과) extract_claims → assign_roles
                         │                                      └─ (누락 검사 실패) END
                         └─ (문서 없음) evidence
assign_roles → [retrieve_t1 ∥ retrieve_t2] → evidence → verify_overlap → compare → draft_spec → approval
approval ─ approved → finalize → END  /  revise → draft_spec  /  rejected → END
```

| 단계 | 노드 | 하는 일 | LLM |
|---|---|---|---|
| ① | `posting_profile` | 공고 → 직군·업종·요구 역량 (온톨로지 조회) | 없음 |
| ② | `ingest` | PDF(텍스트층·스캔 OCR)·DOCX·HWPX·TXT 추출, 레이아웃 복원, ⟦칸⟧ 구조화 | 없음 |
| ② | `pseudonymize` | 이름·연락처·학교·회사 등 치환 + 누락 검사. 실패하면 여기서 멈춤 | 없음 (로컬) |
| ② | `extract_claims` | 검증할 주장 추출 (이력서에서만) | 있음 |
| ② | `assign_roles` | 칸 위치로 역할(참여/주도/설계) 규칙 판정 | 없음 |
| ③ | `retrieve_t1` | 주장 → 역량 후보 (패턴 + BM25 RRF, 공고 역량 가산점 → LLM 선택) | 있음 |
| ③ | `retrieve_t2` | 이력서↔자기소개서 연결 — 자기소개서를 쓰지 않아 현재 보류 | — |
| ③ | `evidence` | 역량 → NCS 수행준거·KSA / ESCO 그래프 조인 | 없음 |
| ④ | `verify_overlap` | 공고 역량마다 지원자 주장으로 확인되는지 판정 (근거 주장 필수) | 있음 |
| ④ | `compare` | 선택 경로 + 확인 경로를 규칙으로 합쳐 공고 역량과 대조, 검증 후보 주장 순위 | 없음 |
| ④ | `draft_spec` | 공통 과제(공고 기준 — 이슈형·기능형)·확장 슬롯 + 경험 검증 문항 초안 | 있음 |
| ④ | `approval` | LangGraph `interrupt()` — 승인 / 수정 요청 / 반려 | — |
| ④ | `finalize` | 승인된 명세 확정, 공통 과제 저장 | 없음 |

자세한 그래프·State 설계는 [`.claude/skills/pipeline-graph/SKILL.md`](.claude/skills/pipeline-graph/SKILL.md).

## 공통 과제 유형

공통 과제는 공고 단위로 한 번 만들고, 같은 공고 지원자는 모두 같은 과제를 받습니다.

| | 이슈형 (`incident`) | 기능 개발형 (`feature`) |
|---|---|---|
| 지원자가 하는 일 | 겉 증상을 보고 자료를 골라 요청하며 원인 진단·대응 | 1단계 구현 → 2단계 디버깅 (배포 뒤 운영 증상 진단·수정) |
| 숨긴 것 | 원인 구조, 흔한 처방이 틀리는 함정 | 숨은 요구 — 흔한 구현은 숨은 테스트에서 실패 |
| 만드는 것 | 명세 (상황 글·자료·관찰 항목·행동 기준) | 명세 + 시작 코드·숨은 테스트·응시용/채점용 이미지 |
| 생성 프롬프트 | `draft_task` | `draft_feature_task` (+ 빌드 생성기) |

- 유형은 공고 주 역량으로 고릅니다 — 진단 역량이 만드는 역량보다 많을 때만 이슈형. 규칙 데이터는 `src/task_spec/resources/task_kind/<직군>.json`.
- 기능형 기본값은 `FEATURE_TASK_DEFAULT`(기본 `false`)로 켭니다. 꺼져 있으면 규칙이 기능형이어도 이슈형으로 만들고 명세에 표시합니다.
- 승인자는 수정 요청(`revise_target=common`)에 `task_kind` 를 넣어 유형을 바꿀 수 있습니다.
- 확장 슬롯·경험 검증 과제는 이슈형만 씁니다.
- 새로 만드는 기능형은 질문·자료 요청 횟수 한도가 없습니다 (2026-11-16, `analysis-stage.md` §4.4).
- 기능형 1단계를 "분석 + 구현"으로 바꾸는 **문제 분석 판**(요청 글·요청자 지식, #151)은 스키마와 생성 단계(`draft_analysis_request`)까지 들어왔고 평가 중입니다.
  `CURRENT` 가 없어 운영 생성에는 아직 쓰지 않습니다.

## 과제 생성 (builder)

승인된 **기능형** 공통 과제만 대상입니다. 명세 승인과 과제 생성은 따로입니다 — API 는 요청을 큐에 넣기만 하고, builder 가 가져가 처리합니다.

| 단계 | 하는 일 |
|---|---|
| contract | 재료(`source_incident`) → 직군 템플릿(`CURRENT`) → `plan_contract` 로 공개 계약·숨은 테스트 계획 |
| build | 시작 코드·공개 테스트·숨은 테스트·참조 구현(흔한 구현·허용 설계) 생성 + 수리 루프. 검증은 `--network none` 자식 컨테이너에서. 지원자 README 렌더 (LLM 없음) |
| images | 응시용·채점용 이미지 → 응시용 오프라인 테스트·참조 구현 채점 → 응시용 레이어 금지 경로 검사 |
| package | 역할별 묶음(zstd)·descriptor(승인 전 `pending`)·`SHA256SUMS` 등록 → `review` |

- 검증 기준: 시작 코드가 빌드되고 기존 테스트가 통과하며, **흔한 구현은 숨은 테스트에서 의도한 사유로 실패하고 허용 설계는 통과**해야 합니다.
  여기에 돌연변이 관문 두 개를 더 봅니다 — 공개 기준 하나만 어긴 변형을 공개 테스트가 잡는지(⑩, `gen_variant`),
  허용 설계를 그 함정에 빠뜨린 변형을 숨은 테스트가 잡는지(⑪, `gen_trap`). 돌연변이 생성기는 Gemini 를 씁니다 (`GEMINI_API_KEY`).
- 응시용 이미지에는 템플릿 `project/`·의존성 캐시 + 시작 코드 + README 만, 숨은 테스트·하네스·`manifest.yaml` 은 채점용 이미지에만 들어갑니다. 참조 구현은 어느 이미지에도 넣지 않습니다.
- 빌드 승인 뒤 builder 가 묶음을 `approved` 로 확정하고 평가 키트를 만듭니다. 전달은 확정된 빌드만 받습니다.
- 비용 상한: 작업당 `BUILD_BUDGET_USD`, 하루 `BUILD_DAILY_BUDGET_USD`.

시작 코드 템플릿은 `templates/<직군>/<스택>/vN` + `CURRENT` 로 버전을 고정합니다 — 지금 `backend/java-spring-boot3-jpa`, `frontend/react-vite-ts`, `data/python-duckdb`.
규칙은 [`templates/README.md`](templates/README.md).

## 빠른 시작 (로컬 개발)

### 요구 사항

- Python 3.11+, [uv](https://docs.astral.sh/uv/)
- podman (또는 docker) — PostgreSQL 16 + pgvector 컨테이너. builder 는 rootless podman 으로 검증 컨테이너·이미지를 만듭니다
- tesseract-ocr (`kor`, `eng`) — 스캔 PDF 를 다룰 때만
- 온톨로지 산출물 (`ontology.sqlite`, `schema_pg.sql`) — 온톨로지 레포의 버전 고정 릴리스
- OpenAI · OpenCode API 키 (과제 생성의 돌연변이 생성기를 돌리려면 Gemini 키도)

### 1. 의존성 설치

```bash
uv sync
```

### 2. DB 기동

```bash
TASK_DB_PASSWORD=<pw> docker/db/run.sh     # task_db 컨테이너, 127.0.0.1:5434
```

`task_db`(운영·로컬)와 `task_db_test`(통합 테스트) 두 DB 가 만들어집니다. 자세한 내용은 [`docker/db/README.md`](docker/db/README.md).

### 3. 환경 변수

```bash
cp .env.example .env
# OPENAI_API_KEY, OPENCODE_API_KEY, OPENCODE_BASE_URL, DATABASE_URL, TEST_DATABASE_URL 채우기
# 과제 생성까지 돌릴 때는 GEMINI_API_KEY 도
```

`.env` 는 앱이 pydantic-settings 로 직접 읽습니다. 전체 항목과 설명은 [`.env.example`](.env.example) 에 있습니다 (builder 설정 `BUILD_*` 포함).

### 4. 온톨로지 적재

온톨로지 레포 산출물을 `data/ontology/` 아래에 두고 PostgreSQL 에 버전별 스키마로 적재합니다.

```bash
uv run python -m task_spec.ontology.load_pg \
  --sqlite data/ontology/src/data/build/ontology.sqlite \
  --schema-sql data/ontology/src/scripts/db/schema_pg.sql \
  --rules-root data/ontology/src
# --rules-root: 새 공고 매칭 규칙도 같은 버전으로 적재 (POST /postings 에 필요)
# 통합 테스트 DB 에도 적재하려면 --target test
```

적재한 버전은 `public.ontology_registry` 에 기록됩니다. `.env` 의 `ONTOLOGY_VERSION` 이 적재된 버전과 맞지 않으면 앱이 뜨지 않습니다 (`dev` = 가장 최근 적재 버전, 개발용).

### 5. 서버 실행

```bash
uv run uvicorn task_spec.api.app:app --port 8000
curl -s http://127.0.0.1:8000/healthz
```

실행 상태와 SSE 를 프로세스 안에서 관리하므로 **워커는 1개**로 띄웁니다.
`UI_ENABLED=true` 면 내부 시험용 화면이 뜹니다 — `/ui/`(공고 → 실행 → 명세 승인), `/ui/builds.html`(과제 생성·빌드 승인).

### 6. builder 실행 (과제 생성을 돌릴 때만)

```bash
uv run python -m task_spec.builder           # 큐를 계속 가져감
uv run python -m task_spec.builder --once    # 작업 하나만 처리하고 끝냄
```

작업 폴더(`BUILD_WORK_DIR`, 기본 `data/builder`)와 묶음 폴더(`IMAGES_DIR`, 기본 `data/images`)는 git 밖입니다.

## API

### 명세

| 메서드 | 경로 | 설명 |
|---|---|---|
| `POST` | `/postings/suggest` | 새 공고 원문(`text` 또는 `file`) → 절 분리 + 직군·업종 후보 |
| `POST` | `/postings` | 확인한 직군·업종으로 임시 공고 생성 (`posting_id = prov-…`) |
| `GET` | `/postings` | 공고 검색 (`q`, `pilot_only`, `limit`) |
| `GET` | `/postings/{posting_id}` | 임시 공고 프로필 |
| `POST` | `/runs` | 실행 생성 (multipart: `posting_id`, `resumes[]`) → `202` + `run_id` |
| `GET` | `/runs?status=&company=&limit=` | 실행 목록 — 최근에 시작한 것부터, `limit` 이 없으면 전부. 상태·공고·회사(id·표기·온톨로지 회사인지)·시작/마지막 시각만. `status` 여러 번 가능, `company` 는 회사 id 또는 표기 |
| `GET` | `/runs/{run_id}` | 실행 상태 (`running` · `awaiting_approval` · `done` · `rejected` · `failed`), 승인 대기 초안·요약 |
| `GET` | `/runs/{run_id}/events` | 진행 상황 SSE |
| `POST` | `/runs/{run_id}/approve` | 승인 결정으로 그래프 resume |
| `GET` | `/runs/{run_id}/spec` | 승인 확정본 (확정 전에는 404) |
| `GET` | `/healthz` | DB·온톨로지 버전·LLM 키 설정 여부 |

업로드 형식은 `.pdf` `.docx` `.hwpx` `.txt` 이고, 파일 1개당 기본 10 MB 까지 받습니다.

```bash
# 1) 실행 생성 — 가상 이력서로
curl -s -X POST http://127.0.0.1:8000/runs \
  -F posting_id=<공고 id> -F resumes=@sample_resume.pdf

# 2) 승인 대기(awaiting_approval)가 될 때까지 상태 확인
curl -s http://127.0.0.1:8000/runs/<run_id>

# 3) 승인 / 수정 요청(revise, comment 필수) / 반려
curl -s -X POST http://127.0.0.1:8000/runs/<run_id>/approve \
  -H 'Content-Type: application/json' \
  -d '{"status":"approved","reviewer":"<승인자>","at":"2026-11-03T10:00:00+09:00"}'
```

`revise` 일 때는 `revise_target` 으로 다시 만들 범위를 고릅니다: `common`(공통 과제) · `verification`(경험 검증 문항만) · `all`(기본).

### 과제 생성·전달

| 메서드 | 경로 | 설명 |
|---|---|---|
| `POST` | `/common-tasks/{common_id}/builds` | 과제 생성 요청 — 큐에 넣기만. 승인된 기능형 공통 과제만 (아니면 409), 진행 중 빌드가 있으면 그 빌드 (200) |
| `GET` | `/builds?common_id=&company=&limit=` | 빌드 목록 — `common_id` 가 없으면 최근 빌드부터, `limit` 이 없으면 전부. 회사 칸(`company_id`·`company`·`company_known`)과 `company` 거르기는 `GET /runs` 와 같은 규칙 (공통 과제 → 공고 → 회사) |
| `GET` | `/builds/{build_id}` | 상태·검증 요약·검토 표시·파일 수 |
| `GET` | `/builds/{build_id}/approval` | 빌드 승인 — `review` 빌드면 승인 대기를 열고 검토 표시·사유, 끝난 빌드면 결과 |
| `POST` | `/builds/{build_id}/approve` | 승인·반려로 그래프 resume |
| `GET` | `/builds/{build_id}/artifacts` | 역할별 묶음 파일(이름·sha256·크기)·이미지 id |
| `GET` | `/builds/{build_id}/readme` | 지원자 README (응시용 이미지 안의 README 와 같음) |
| `GET` | `/builds/{build_id}/facilitator` | 진행자(응시 플랫폼) 보기 — 요청하면 주는 자료, 질문 답, 2단계 |
| `GET` | `/builds/{build_id}/cases` | 승인자 보기 — 숨은 케이스·허용 설계 (지원자·평가 쪽에 내주지 않음) |
| `GET` · `POST` | `/builds/{build_id}/deliveries` | 전달 기록 · SFTP 전달 요청 (승인·확정된 빌드만, `202` 뒤 폴링) |
| `GET` · `POST` | `/delivery-targets` | 전달 대상 (이름 → 받을 역할) |
| `GET` | `/pull/builds` | 받는 쪽이 가져갈 수 있는 빌드 (승인·확정된 것) — 파일 목록·받았음 표시 |
| `GET` | `/pull/builds/{build_id}/files/{name}` | 묶음 파일 하나 내려받기 (`SHA256SUMS` 는 DB 기록으로 만듦) |
| `POST` | `/pull/builds/{build_id}/done` | 다 받고 확인했음 → 전달 기록 |

`/pull` 은 Basic 인증 대신 받는 곳별 Bearer 토큰(`PULL_TOKENS__<이름>`)을 보고, 그 대상의 역할 하나만 엽니다. SFTP 전달과 받는 곳마다 고릅니다.
계약 원문·숨은 계획 원문·테스트 코드·참조 구현 본문은 어느 응답에도 칸이 없습니다.

> 자기소개서(`cover_letters[]`)는 기본 설정(`USE_COVER_LETTERS=false`)에서 저장·처리하지 않고 응답 `ignored` 로만 알립니다.
> 앱 자체에는 아직 인증이 없습니다. 사내망 밖에 둘 때는 nginx(TLS + Basic 인증)를 앞에 둡니다 — [`docs/deploy.md`](docs/deploy.md).

## 테스트

```bash
uv run pytest -q                    # 단위 테스트 (기본: pg·llm·podman 제외)
uv run pytest -q -m pg              # PostgreSQL 통합 테스트 — TEST_DATABASE_URL 필요 (DATABASE_URL 과 달라야 함)
uv run pytest -q -m llm             # 실제 LLM API 스모크 — 비용 발생, 프롬프트·llm 변경 때만
uv run pytest -q -m podman          # 실제 podman 자식 컨테이너 격리 확인
uv run ruff check . && uv run ruff format --check .
```

## 평가

`eval/` 에 단계별 채점 스크립트가 있습니다. 표본은 모두 가상 데이터·시범 공고이고 dev/test 로 나눠 씁니다.
리포트는 `eval/<단계>/reports/` 또는 `data/eval/`(git 밖)에 남습니다.

| 단계 | 명령 예 |
|---|---|
| 입력 추출 | `uv run python -m eval.ingest.run_eval --split dev --show-misses` |
| 가명화 | `uv run python -m eval.pseudonymize.run_eval --split dev --show-misses` |
| 주장 추출 | `uv run python -m eval.claims.run_eval --split dev --models opencode:qwen3.8-flash` |
| 역량 검색 | `uv run python -m eval.retrieve.run_eval --split dev` · `eval.retrieve.run_select` · `run_overlap` |
| 새 공고 | `uv run python -m eval.postings.run_eval --split dev --llm` |
| 명세 | `eval.spec.run_draft` · `run_feature` · `run_slots` · `run_solo` · `run_trap_facts` · `run_fact_leak` · `compare_versions` |
| 과제 생성 | `uv run python -m eval.starter.run_alpha --posting <공고 id> --budget-usd 0.5` (공고 → 기능형 명세 → 계약 → 생성·수리 → 응시용 프로젝트) |
| 문제 분석 손 시범 | `eval.pilot.prepare` · `simulate` · `grade` · `analyze` — `analysis-stage.md` §9.1 판별 평가 |

LLM 을 부르는 평가는 비용이 듭니다. 절차와 기준치는 `.claude/skills/eval-harness/SKILL.md` 를 봅니다.

## 프롬프트

```
prompts/<task>/
  v1.md, v2.md, …   # 머리말(모델·설정·스키마·평가 결과·변경 이유) + 본문. 기존 버전은 고치지 않는다
  CURRENT           # 지금 쓰는 버전. 평가를 통과한 뒤에만 바꾼다
  PROMOTIONS.md     # 승격 기록
```

| task | 용도 |
|---|---|
| `extract_claims` | 이력서에서 검증할 주장 추출 |
| `select_competency` | 역량 후보 중 0~2개 선택 |
| `verify_overlap` | 공고 역량 → 지원자 주장 확인 |
| `draft_task` | 이슈형 공통 과제 서술 |
| `draft_feature_task` · `fix_feature_fields` | 기능형 공통 과제 서술 · 재요청 뒤 남은 '보이면 안 되는 말'만 부분 수리 |
| `draft_analysis_request` | 문제 분석 판 요청 글·요청자 지식 (평가 중 — `CURRENT` 없음) |
| `draft_slot` · `slot_questions` | 확장 슬롯 (공고 단위) · 지원자별 연결 문구·면접 확인 질문 |
| `segment_posting` · `suggest_field` | 새 공고 절 분리 · 직군·업종 추천 |
| `plan_contract` | 공개 계약·숨은 테스트 계획 (과제 생성) |
| `gen_starter` · `gen_public` · `gen_hidden` · `gen_naive` · `gen_accepted` | 시작 코드·공개 테스트·숨은 테스트·흔한 구현·허용 구현 (기본 Java, 스택별 `_py`·`_react`) |
| `gen_variant` · `gen_trap` | 빌드 돌연변이 관문 ⑩·⑪ (Gemini) |
| `judge_draft_task` · `judge_overlap` · `judge_trap_facts` · `judge_fact_leak` · `solve_task` · `solve_select` · `grade_solo` | 평가 전용 |
| `facilitator_choose` · `expand_match_terms` | 평가 전용 — 문제 분석 손 시범의 진행자 흉내·동의어 목록 |

앱은 기동할 때 모든 `CURRENT` 프롬프트를 검증하고, 문제가 있으면 뜨지 않습니다.
예외로 시작 코드 빌드 생성기(`gen_*` 와 스택별 `_py`·`_react`, `plan_contract`)는 새 vN 을 만들면 같은 변경에서 `CURRENT` 를 올리고 `PROMOTIONS.md` 에 근거를 적습니다.
규칙: `.claude/skills/prompt-registry/SKILL.md`.

## 디렉토리

```
src/task_spec/
  api/          FastAPI — 라우터(runs·postings·builds), 업로드, 백그라운드 실행기, SSE, 임시 화면(static/)
  graph/        LangGraph — 명세 그래프, 빌드 승인 그래프, 체크포인터 (AsyncPostgresSaver)
  nodes/        파이프라인 노드 (ingest/, pseudonymize/, claims, retrieve, verify_overlap, compare, spec, starter/ …)
  llm/          모델 호출 래퍼 — 재시도, 구조화 출력, 비용 기록, 인용 대조
  prompting/    프롬프트 레지스트리·조립·provenance
  ontology/     온톨로지 적재(load_pg)·읽기 전용 조회
  postings/     새 공고 절 분리·매칭·임시 공고 저장
  retrieve/     BM25·패턴 검색, 역량 선택
  spec/         공통 과제 골격·유형 선택·서술·검사·부분 수리, 문제 분석 판, 확장 슬롯, 공통 과제 저장
  starter/      과제 생성 — 템플릿·계약·검증기·이미지·묶음·빌드 저장소·진행자 보기·평가 키트
  builder/      과제 생성 worker — 큐 lease, 단계 실행, 승인 뒤 묶음 확정
  delivery/     SFTP 전달 (받는 쪽이 가져가는 /pull 은 api/routers/pull.py)
  resources/    유형 규칙(task_kind/), 가명화 사전(dicts/)
  schemas/      Pydantic 데이터 경계 (State, 명세, 승인, 빌드·묶음 등)
prompts/        프롬프트 버전 (위 참고)
templates/      시작 코드 템플릿 (<직군>/<스택>/vN + CURRENT)
incidents/      공개 사례 — 이슈형 원리·함정, 기능형 재료(feature 블록) (지원자 데이터 아님)
eval/           평가 스크립트·리포트 (starter/ = 과제 생성 알파, pilot/ = 문제 분석 손 시범)
tests/          pytest (가상 데이터만)
docker/         api · db · builder · nginx 컨테이너, 릴리스 빌드 스크립트
docs/           배포 가이드, 설계 문서(design/), Claude Code 설정 카탈로그
data/           git 밖 — 원본·가명화본·실행 결과·온톨로지 산출물·빌드 작업 폴더·과제 묶음·시크릿
```

## 설계 문서

| 문서 | 내용 |
|---|---|
| [`task-spec-schema.md`](docs/design/task-spec-schema.md) | 과제 명세 스키마 (SSOT — `schemas/domain.py` 와 같은 PR 에서 고침) |
| [`feature-task-backend.md`](docs/design/feature-task-backend.md) | 기능 개발형 공통 과제 — 유형 선택, 숨은 요구, 두 단계, 평가 |
| [`starter-templates.md`](docs/design/starter-templates.md) | 시작 코드 템플릿·검증·응시용/채점용 이미지·전달 |
| [`spec-to-code.md`](docs/design/spec-to-code.md) | 명세 → 계약 → 시작 코드 생성 파이프라인 |
| [`code-skeleton.md`](docs/design/code-skeleton.md) | 코드 뼈대 — 계약에서 코드가 먼저 쓰고 생성기는 몸통만 |
| [`analysis-stage.md`](docs/design/analysis-stage.md) | 기능형 1단계에 문제 분석 합치기 — 진행자/AI 두 채널, 한도 없음 [제안, #151 — 평가 중] |

## 배포

운영 서버(rootless podman, 내부망 반입) 절차는 [`docs/deploy.md`](docs/deploy.md), 규칙은 `.claude/skills/deployment/SKILL.md` 에 있습니다.
API·DB·nginx 외에 과제 생성 builder(전용 사용자 `task-builder`, 2-8)·재부팅 뒤 다시 띄우기(2-9)·builder 자동 업그레이드(2-10)·
SFTP 전달 대상(2-11)·받는 쪽이 가져가는 전달 `/pull`(2-12)도 같은 문서에 있습니다.
API 와 builder 를 한 번에 올릴 때는 `docker/deploy_all.sh [--api-only|--builder-only]`(2-13) — 커밋 안 된 변경이 있으면 거부하고, `/healthz` 가 실패하면 이전 이미지로 되돌립니다.

## 개발 규칙

- `main` 에 직접 커밋하지 않습니다. 브랜치에서 작업하고 PR 로 합칩니다. 원격은 ssh(`git@github.com:JangYunSeong/Wing-C-Assignment.git`).
- 커밋 메시지는 Conventional Commits(한국어)를 따릅니다. 규칙은 `.claude/agents/git-committer.md`.
- `from __future__ import annotations`, 타입 힌트 필수, 노드 간 데이터는 `schemas/` 의 Pydantic 모델로 주고받습니다.
- 로그에는 원문이나 가명화 매핑을 남기지 않습니다 (id·해시·길이만).
- Claude Code 스킬·에이전트·훅 목록: [`docs/claude-config-catalog.md`](docs/claude-config-catalog.md)
