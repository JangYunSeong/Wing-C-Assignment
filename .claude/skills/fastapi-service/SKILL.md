---
name: fastapi-service
description: P1 과제 명세 서비스의 FastAPI 레이어 규칙. 실행 생성(POST /runs), 상태 조회, SSE 진행 스트림, 승인 resume(POST /runs/{id}/approve), 업로드 처리, lifespan 에서 그래프·체크포인터 수명 관리, 에러 응답 형식을 다룬다. src/task_spec/api/ 를 만들거나 고칠 때, 엔드포인트를 추가할 때, 그래프를 API 에 연결할 때 사용. FastAPI 일반 문법은 fastapi 스킬, async 는 async-python-patterns 스킬을 함께 본다.
---

# fastapi-service — API 레이어

FastAPI 문법·관례는 vendored `fastapi` 스킬(`Annotated` 의존성, `EventSourceResponse`, 라우터)을 따른다.
이 스킬은 **이 서비스의 엔드포인트 계약과 그래프 연결 규칙**만 정한다.

## 레이어 경계

```
api/        라우터·요청/응답 스키마·의존성. 비즈니스 로직 없음
  └─ graph/ 를 호출 (graph.ainvoke / astream / Command(resume=...))
```

- 라우터는 노드를 직접 부르지 않는다. 항상 그래프를 통해서.
- 응답에 원문·가명화 매핑을 담지 않는다. 명세·주장은 **가명화된 값**만.

## 엔드포인트

| 메서드 | 경로 | 동작 |
|---|---|---|
| `POST` | `/runs` | 공고 id + 업로드 문서 (+ `career_level`) → `run_id` 발급, 백그라운드 실행 시작. `202`. 공고에 신입·경력이 정해져 있지 않으면 `career_level`(신입·경력)이 필요(`422 career_level_required`)하고 받은 값은 공고에 저장(`set_by=run:<run_id>`), 정해져 있으면 비우거나 같은 값만(`409 career_level_conflict` — 바꾸려면 `PUT /postings/{id}/career-level`). 거절은 지원서를 저장하기 전에. 공고 저장소가 없는 앱(테스트)은 보지 않음 [확정: 2026-11-19 사용자 — 명세 전에 신입·경력] |
| `GET` | `/runs?status=&company=&limit=` | 실행 목록 — 최근에 시작한 것부터, `limit` 이 없으면 전부 [확정: 2026-11-18 사용자 — 회사의 명세를 전부]. thread 첫 체크포인트(`source=input`)와 그 그래프 입력(공고 id)으로 찾고 run_id(uuid4 hex) 꼴만 (빌드 승인 thread 제외), 상태는 `GET /runs/{id}` 와 같은 계산. 회사(`postings/company.py`): id = 표기에서 공백·법인 표기를 빼고 소문자 — 온톨로지에 없는 새 회사도 id 가 생기고, 표시 이름은 온톨로지 `company.name` 과 같으면 그 표기, `company_known` = 온톨로지 회사인지. `company` 로 거르기는 상태를 읽기 전에. 상태·현재 노드·판·공고·회사·시작/마지막 시각만 — 명세 초안·승인 요약 없음 [확정: 2026-11-18 사용자 — Wing 관리 화면의 '최근 실행'] |
| `GET` | `/runs/{run_id}` | 상태(`running`/`awaiting_approval`/`done`/`rejected`/`failed`) + 현재 노드 + (승인 대기 시) 명세 초안 + 승인 요약(`approval_summary` — 승인 대기·`done`·`rejected`, `spec/summary.py`) |
| `GET` | `/runs/{run_id}/events` | SSE. 노드 진행·interrupt 발생 이벤트 |
| `POST` | `/runs/{run_id}/approve?revision=` | `ApprovalDecision` 로 `Command(resume=...)`. 승인 대기 상태가 아니면 `409` (`approval_not_pending` — 같은 실행에 결정이 겹쳐 다른 결정이 먼저 띄웠으면, 그 결정이 이미 끝났어도). `revision`(승인자가 보고 있는 판, 화면이 보낸다)이 지금 승인 대기 판과 다르면 `409` (`approval_revision_mismatch`) — 판 번호는 체크포인트에 실리는 `ApprovalDecision` 이 아니라 요청에만 둔다 (#247) |
| `GET` | `/runs/{run_id}/spec` | 확정 명세 (승인된 경우만, 아니면 `404`) |
| `GET` | `/builds?common_id=&company=&limit=` | 빌드 목록 — 최근 빌드부터, `limit` 이 없으면 전부. 회사 칸·`company` 거르기는 `GET /runs` 와 같은 규칙(`postings/company.py`, 공통 과제의 공고로) — 빌드 상세(`/builds/{id}`)에도 회사 칸 [확정: 2026-11-18 사용자 — 빌드도 선택한 회사만] |
| `GET` | `/builds/{build_id}/approval` | 빌드(시작 코드) 승인 (#130) — review 빌드면 빌드 승인 그래프 thread(`build-<build_id>-<결과 키>`)를 시작해 interrupt 까지, 검토 표시·사유. 끝난 빌드는 결과 |
| `POST` | `/builds/{build_id}/approve` | `BuildApprovalDecision` 으로 resume 만. review 가 아니면 `409`, 그래프가 다시 기다리면(판단 누락·저장소 규칙) `422` + 사유 |
| `GET` | `/healthz` | 프로세스·Postgres 연결·온톨로지 산출물 버전 (키 값은 노출하지 않고 설정 여부만) |
| `GET` | `/postings?q=` | 공고 찾기 — 회사·직무명·id·직군 이름으로 임시 공고와 온톨로지 공고(기본 시범만, `pilot_only`), 최대 `limit` 건. 검색어는 공백으로 나눠 AND, LIKE 특수문자는 글자 그대로 [제안: 2026-11-04] |
| `GET` | `/postings/{id}` | 공고 프로필 — 임시 공고(`prov-…`)와 온톨로지 공고 모두 (`#` 이 든 id 는 URL 인코딩) |
| `PUT` | `/postings/{id}/career-level` | 공고의 신입·경력을 사람이 정함 `{career_level: "신입"|"경력"|null, set_by}` — null 이면 지움. 값은 task_db `posting_meta`(온톨로지 공고도 — 온톨로지는 읽기만). 공고 확인(`POST /postings`)의 `career_level` 도 같은 곳에. 공고 찾기·프로필·`/builds`·`/runs` 가 `career_level`·`career_level_source`(`confirmed` 사람이 정함 / `posting_text` 직무명·자격요건 문구 추정 / null) 로 같은 값을 보이고, 추천(`/postings/suggest`)은 `career_level_candidate` 로 추정값을 준다. 과제 생성에는 쓰지 않는다 (`postings/career.py`) [확정: 2026-11-19 사용자 — 두 값, 추정값도 내보냄, Wing 이 과제를 신입·경력으로 나눔] |

- 상태는 `graph.aget_state(config)` 에서 계산한다. 별도 상태 테이블을 두지 않는다 (체크포인터가 SSOT).
- 승인 대기 판정: `state.tasks` 에 interrupt 가 있으면 `awaiting_approval`.

## 실행 방식

- CPU-only 서버라 실행이 길다(LLM 호출 수십 초~분, `context.md` §7) → **요청 안에서 끝까지 돌리지 않는다.**
  `POST /runs` 는 실행을 백그라운드 태스크로 띄우고 즉시 반환.
- 초기: `asyncio.create_task` + 프로세스 내 태스크 레지스트리. 다중 워커가 필요해지면 작업 큐로 교체 [미정].
  교체가 쉬도록 `RunLauncher` 인터페이스 하나로 감싼다.
- 승인 resume 은 상태를 읽기 **전에** `launcher.launches(run_id)` 를 받아 `launch(after=…)` 로 넘긴다 — 읽은 뒤 다른 요청이
  띄웠으면(먼저 들어온 결정이 이미 끝났어도) `RunBusy` → `409` (#247). 프로세스 안 레지스트리라 워커 하나(`--workers 1`)에서만 막는다.
- 그래프 `config = {"configurable": {"thread_id": run_id}}`.
- 인터뷰 과제 만들기도 같은 방식 (#280) — `api/interview_generation.InterviewGenerationRunner` 가 `RunLauncher` 인스턴스를 하나 더 둔다.
  서비스는 `interview/generation.GenerationRunner` 프로토콜로만 안다 (인터뷰 모듈이 API 계층을 import 하지 않게). lifespan 에서 같은 체크포인터로
  만들어 `app.state.interviews.generation` 에 넣고 종료 때 `shutdown()`. 진행 판정은 `launcher.thread_status` — 체크포인트가 중간인데 이 프로세스에서
  안 돌면 interrupted (이어서 하기 `POST /interviews/{id}/generation/resume`).

## lifespan

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with make_checkpointer(settings) as cp:      # pipeline-graph 의 팩토리
        app.state.ontology = load_ontology_artifact(settings.ontology_version)  # 버전 검증 실패 시 기동 실패
        app.state.graph = build_graph(checkpointer=cp)
        app.state.launcher = RunLauncher(app.state.graph)
        yield
        await app.state.launcher.shutdown()
```

의존성은 `Annotated` 별칭으로: `GraphDep = Annotated[CompiledStateGraph, Depends(get_graph)]`.

## 업로드

- 받은 파일은 즉시 `data/raw/<run_id>/` 에 저장하고 해시만 State 로 넘긴다.
- 허용 형식: PDF, DOCX, TXT [제안]. 크기 상한 설정값으로.
- 업로드 원본을 응답·로그에 되돌리지 않는다.
- 자기소개서(`cover_letters`)는 `USE_COVER_LETTERS=false`(기본)면 **저장하지 않는다** — 응답 `ignored: ["cover_letter#1", …]` 로만 알린다
  (파일 이름은 넣지 않음). 검증할 주장은 이력서에서만 [확정: 2026-11-03].

## 에러 형식

```json
{"error": {"code": "approval_not_pending", "message": "...", "run_id": "..."}}
```

- 4xx: 입력·상태 오류. 5xx: 내부 오류 — 메시지에 원문 조각 포함 금지.
- 예외 핸들러 한 곳에서 도메인 예외 → 코드 매핑.

## 화면 [확정: 2026-11-20 사용자 — 내부 시험용 /ui/ 제거]

- 화면은 Wing 관리 화면이 맡는다. 이 API 는 화면 파일을 내주지 않고, 화면 전용 엔드포인트를 만들지 않는다.
- 예전 임시 화면(`/ui/`, `api/static/`, `UI_ENABLED`)은 Wing 이 같은 일(공고·실행·명세 승인·빌드 승인·인터뷰)을 하게 되어 지웠다.

## 설정

`pydantic-settings` 로 `Settings` 하나. `.env` 는 앱이 직접 로드한다 (셸에서 열지 않음, 훅이 막는다).
필수: `ONTOLOGY_VERSION`, `ONTOLOGY_ARTIFACT_DIR`, `DATA_DIR`, `DATABASE_URL`(Postgres),
`OPENAI_API_KEY`, `OPENCODE_API_KEY`, `OPENCODE_BASE_URL`. 테스트: `TEST_DATABASE_URL`. 목록은 `.env.example` 이 SSOT.

## 인증

[미정]. 승인 API 는 반드시 승인자 식별(`reviewer`)이 필요하므로, 인증 전까지는 요청 본문 `reviewer` 를 받고
TODO 로 표시한다.

## 테스트

- `httpx.AsyncClient` + `ASGITransport`, 그래프는 가짜 LLM + `InMemorySaver`.
- 시나리오: 생성 → 승인 대기 → approve → spec 조회 / 승인 대기 아닌 상태에서 approve → 409.
