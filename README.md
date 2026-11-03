# task-spec — P1 과제 명세 생성

이력서 + 채용 공고 + 직무 역량 온톨로지를 받아 **과제 명세(task_spec)** 초안을 만들고,
**사람이 승인해야만** 확정하는 서비스입니다. FastAPI(인터페이스) 위에서 LangGraph 파이프라인이 돕니다.

- 명세 스키마의 기준: 온톨로지 레포 `JangYunSeong/job-competency-ontology` 의 `context.md` §6
- 고영향 AI(AI 기본법)로 다루므로 최종 판단은 사람이 합니다. 승인 없이 명세가 확정되는 경로는 없습니다.
- 규칙·불변식 전체는 [`CLAUDE.md`](CLAUDE.md) 에 있습니다. 이 README 는 개요와 실행 방법만 다룹니다.

## 파이프라인

```
START → posting_profile ─┬─ (문서 있음) ingest → pseudonymize ─┬─ (통과) extract_claims → assign_roles
                         │                                      └─ (누락 검사 실패) END
                         └─ (문서 없음) evidence
assign_roles → [retrieve_t1 ∥ retrieve_t2] → evidence → compare → draft_spec → approval
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
| ④ | `compare` | 공고 역량과 지원자 역량 대조, 검증 후보 주장 순위 | 없음 |
| ④ | `draft_spec` | 공통 과제(공고 기준) + 경험 검증 과제 초안 | 있음 |
| ④ | `approval` | LangGraph `interrupt()` — 승인 / 수정 요청 / 반려 | — |
| ④ | `finalize` | 승인된 명세 확정 | 없음 |

자세한 그래프·State 설계는 [`.claude/skills/pipeline-graph/SKILL.md`](.claude/skills/pipeline-graph/SKILL.md).

## 핵심 원칙

- **지원자 데이터는 git 에 넣지 않습니다.** 원본·가명화본·실행 결과는 전부 `data/`(gitignore). 테스트 픽스처는 가상 데이터만 씁니다.
- **가명화를 통과한 텍스트만** LLM·임베딩(외부 API 포함)으로 보냅니다. 가명화 단계 자체는 외부 호출을 하지 않습니다.
- **공통 과제는 공고만으로 만듭니다.** 지원서는 주장 추출에만 써서 같은 공고 지원자끼리 비교할 수 있게 합니다.
- **온톨로지는 버전을 고정해 읽기만 합니다.** 모든 명세에 `ontology_version` 이 남습니다.
- **프롬프트는 파일로 버전 관리합니다** (`prompts/<task>/vN.md` + `CURRENT`). LLM 산출물마다 provenance(프롬프트 버전·해시, 모델·설정, 온톨로지 버전)를 남깁니다.

## 빠른 시작 (로컬 개발)

### 요구 사항

- Python 3.11+, [uv](https://docs.astral.sh/uv/)
- podman (또는 docker) — PostgreSQL 16 + pgvector 컨테이너
- tesseract-ocr (`kor`, `eng`) — 스캔 PDF 를 다룰 때만
- 온톨로지 산출물 (`ontology.sqlite`, `schema_pg.sql`) — 온톨로지 레포의 버전 고정 릴리스
- OpenAI · OpenCode API 키

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
```

`.env` 는 앱이 pydantic-settings 로 직접 읽습니다. 전체 항목과 설명은 [`.env.example`](.env.example) 에 있습니다.

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

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| `POST` | `/postings/suggest` | 새 공고 원문(`text` 또는 `file`) → 절 분리 + 직군·업종 후보 |
| `POST` | `/postings` | 확인한 직군·업종으로 임시 공고 생성 (`posting_id = prov-…`) |
| `GET` | `/postings/{posting_id}` | 임시 공고 프로필 |
| `POST` | `/runs` | 실행 생성 (multipart: `posting_id`, `resumes[]`) → `202` + `run_id` |
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

> 자기소개서(`cover_letters[]`)는 기본 설정(`USE_COVER_LETTERS=false`)에서 저장·처리하지 않고 응답 `ignored` 로만 알립니다.
> 앱 자체에는 아직 인증이 없습니다. 사내망 밖에 둘 때는 nginx(TLS + Basic 인증)를 앞에 둡니다 — [`docs/deploy.md`](docs/deploy.md).

## 테스트

```bash
uv run pytest -q                    # 단위 테스트 (기본: pg·llm 제외)
uv run pytest -q -m pg              # PostgreSQL 통합 테스트 — TEST_DATABASE_URL 필요 (DATABASE_URL 과 달라야 함)
uv run pytest -q -m llm             # 실제 LLM API 스모크 — 비용 발생, 프롬프트·llm 변경 때만
uv run ruff check . && uv run ruff format --check .
```

## 평가

`eval/` 에 단계별 채점 스크립트가 있습니다. 표본은 모두 가상 데이터이고 dev/test 로 나눠 씁니다.
리포트는 `eval/<단계>/reports/` 에 남습니다.

| 단계 | 명령 예 |
|---|---|
| 입력 추출 | `uv run python -m eval.ingest.run_eval --split dev --show-misses` |
| 가명화 | `uv run python -m eval.pseudonymize.run_eval --split dev --show-misses` |
| 주장 추출 | `uv run python -m eval.claims.run_eval --split dev --models opencode:qwen3.8-flash` |
| 역량 검색 | `uv run python -m eval.retrieve.run_eval --split dev` · `eval.retrieve.run_select` |
| 새 공고 | `uv run python -m eval.postings.run_eval --split dev --llm` |
| 명세 | `eval.spec.run_draft` · `run_verify` · `run_solo` · `compare_versions` |

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
| `draft_task` | 공통 과제 서술 |
| `verify_task` | 경험 검증 확장 문항 |
| `segment_posting` · `suggest_field` | 새 공고 절 분리 · 직군·업종 추천 |
| `judge_draft_task` · `solve_task` · `solve_select` · `grade_solo` | 평가 전용 |

앱은 기동할 때 모든 `CURRENT` 프롬프트를 검증하고, 문제가 있으면 뜨지 않습니다. 규칙: `.claude/skills/prompt-registry/SKILL.md`.

## 디렉토리

```
src/task_spec/
  api/          FastAPI — 라우터, 업로드, 백그라운드 실행기, SSE
  graph/        LangGraph 그래프 조립, 체크포인터 (AsyncPostgresSaver)
  nodes/        파이프라인 노드 (ingest/, pseudonymize/, claims, retrieve, compare, spec …)
  llm/          모델 호출 래퍼 — 재시도, 구조화 출력, 비용 기록, 인용 대조
  prompting/    프롬프트 레지스트리·조립·provenance
  ontology/     온톨로지 적재(load_pg)·읽기 전용 조회
  postings/     새 공고 절 분리·매칭·임시 공고 저장
  retrieve/     BM25·패턴 검색, 역량 선택
  spec/         공통 과제 골격·서술·검증, 공통 과제 저장
  schemas/      Pydantic 데이터 경계 (State, 명세, 승인 등)
prompts/        프롬프트 버전 (위 참고)
incidents/      공개 사례 — 공통 과제의 원리·함정 재료 (지원자 데이터 아님)
eval/           평가 스크립트·리포트
tests/          pytest (가상 데이터만)
docker/         api · db · nginx 컨테이너, 릴리스 빌드 스크립트
docs/           배포 가이드, Claude Code 설정 카탈로그
data/           git 밖 — 원본·가명화본·실행 결과·온톨로지 산출물·시크릿
```

## 배포

운영 서버(podman, 내부망 반입) 절차는 [`docs/deploy.md`](docs/deploy.md), 규칙은 `.claude/skills/deployment/SKILL.md` 에 있습니다.

## 개발 규칙

- `main` 에 직접 커밋하지 않습니다. 브랜치에서 작업하고 PR 로 합칩니다.
- 커밋 메시지는 Conventional Commits(한국어)를 따릅니다. 규칙은 `.claude/agents/git-committer.md`.
- `from __future__ import annotations`, 타입 힌트 필수, 노드 간 데이터는 `schemas/` 의 Pydantic 모델로 주고받습니다.
- 로그에는 원문이나 가명화 매핑을 남기지 않습니다 (id·해시·길이만).
- Claude Code 스킬·에이전트·훅 목록: [`docs/claude-config-catalog.md`](docs/claude-config-catalog.md)
