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
| `POST` | `/runs` | 공고 id + 업로드 문서 → `run_id` 발급, 백그라운드 실행 시작. `202` |
| `GET` | `/runs/{run_id}` | 상태(`running`/`awaiting_approval`/`done`/`rejected`/`failed`) + 현재 노드 + (승인 대기 시) 명세 초안 |
| `GET` | `/runs/{run_id}/events` | SSE. 노드 진행·interrupt 발생 이벤트 |
| `POST` | `/runs/{run_id}/approve` | `ApprovalDecision` 로 `Command(resume=...)`. 승인 대기 상태가 아니면 `409` |
| `GET` | `/runs/{run_id}/spec` | 확정 명세 (승인된 경우만, 아니면 `404`) |
| `GET` | `/healthz` | 프로세스·Postgres 연결·온톨로지 산출물 버전 (키 값은 노출하지 않고 설정 여부만) |

- 상태는 `graph.aget_state(config)` 에서 계산한다. 별도 상태 테이블을 두지 않는다 (체크포인터가 SSOT).
- 승인 대기 판정: `state.tasks` 에 interrupt 가 있으면 `awaiting_approval`.

## 실행 방식

- CPU-only 서버라 실행이 길다(LLM 호출 수십 초~분, `context.md` §7) → **요청 안에서 끝까지 돌리지 않는다.**
  `POST /runs` 는 실행을 백그라운드 태스크로 띄우고 즉시 반환.
- 초기: `asyncio.create_task` + 프로세스 내 태스크 레지스트리. 다중 워커가 필요해지면 작업 큐로 교체 [미정].
  교체가 쉬도록 `RunLauncher` 인터페이스 하나로 감싼다.
- 그래프 `config = {"configurable": {"thread_id": run_id}}`.

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
