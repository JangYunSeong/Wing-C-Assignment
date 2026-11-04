---
name: llm-client
description: LLM 호출 공통 래퍼 규칙. 모델 라우팅(로컬/외부), 구조화 출력 검증, 빈 응답·추론 토큰 소진 재시도(qwen3.8-flash reasoning_effort=low), 타임아웃, 토큰·비용·지연 기록, 가명화 타입 강제, 원문 인용 대조 검사, 테스트용 가짜 LLM 을 다룬다. src/task_spec/llm/ 을 만들거나 고칠 때, 새 모델을 붙일 때, LLM 출력이 비거나 깨질 때 사용.
---

# llm-client — 모델 호출

모든 LLM 호출은 `llm.call()` 하나를 지난다. 노드가 모델 SDK 를 직접 만들지 않는다.

```python
result = await llm.call(assembled, output=ClaimExtractionOutput)
# result.parsed, result.provenance
```

`assembled` 는 `prompt-registry` 의 조립 결과(메시지 + params + provenance 초안).

## 프로바이더 [확정: 2026-11-02]

| provider | 클라이언트 | `.env` |
|---|---|---|
| `openai` | `ChatOpenAI` (langchain-openai) | `OPENAI_API_KEY` |
| `opencode` | `ChatOpenAI(base_url=OPENCODE_BASE_URL, api_key=OPENCODE_API_KEY)` — OpenAI 호환 엔드포인트 | `OPENCODE_API_KEY`, `OPENCODE_BASE_URL` |

- 두 프로바이더 모두 **외부 API** 다. 지원자 계열 입력은 `PseudonymizedText` 만 허용 — 아니면 호출 전에 예외.
- 프롬프트 머리말의 `model` 은 `<provider>:<model>` 형식 (예: `opencode:qwen3.8-flash`, `openai:<model>`).
  `LLMClient` 가 provider 접두로 백엔드를 고른다. 모델 설정은 머리말이 SSOT, 코드 기본값으로 덮지 않는다.
- OpenCode 쪽이 지원하지 않는 파라미터(예: `reasoning_effort`)는 provider 별 허용 목록으로 걸러 보내고, 걸러진 사실을 provenance 에 남긴다.
- 구조화 출력 [확정: 2026-11-02]: `with_structured_output` 을 쓰지 않는다. 프로바이더마다 json_schema 지원이 달라서,
  프롬프트에 `{{output_schema}}`(출력 모델 JSON 스키마)를 넣고 응답 본문 JSON 을 직접 파싱(코드 펜스 허용) → Pydantic 검증.
- 구조: `ChatBackend`(1회 호출, `llm/backend.py`) ← `LLMClient`(재시도·검증·provenance, `llm/client.py`).
  실제 백엔드는 `OpenAICompatBackend`(ChatOpenAI, SDK 재시도 0), 조립은 `llm/factory.py` 의 `build_llm_client(settings)`.
- 프로바이더별 허용 파라미터: `ALLOWED_PARAMS` (`llm/client.py`). 걸러진 키는 provenance `params.dropped`.
- 키는 `Settings` 가 `.env` 에서 로드. 키 값을 로그·provenance·예외 메시지·`/healthz` 에 넣지 않는다.
- 비용 계산용 단가는 `llm/pricing.py` 표 하나 (모르는 모델은 `cost_usd: null`).

## 재시도

| 증상 | 처리 |
|---|---|
| 빈 응답 + 출력 토큰이 추론에 소진 | `reasoning_effort=low` 로 1회 재시도 (시험 사실: `qwen3.8-flash` 는 긴 입력에서 기본 추론 설정이면 빈 응답) |
| 5xx·연결 오류·429 | 지수 백오프 최대 3회 |
| 시간 초과 | **1회만** 다시 [확정: 2026-11-03 — 운영에서 120초 × 4회 재시도로 draft_spec 이 수십 분 멈춤]. 요청 제한 시간 `LLM_TIMEOUT_SECONDS`(기본 300초) |
| JSON 파싱·스키마 검증 실패 | 검증 에러 메시지를 붙여 1회 재요청. 그래도 실패면 `SchemaViolation` |
| 4xx(인증·요청 오류) | 재시도 없음 |

재시도 횟수·사유는 provenance `retries` 에 남긴다. 재시도는 WARNING(`llm retry prompt=… model=… reason=… elapsed_ms=…`),
포기는 ERROR(`llm give up …`) 로그 — 본문·키 없이. 로깅 설정은 `logging_setup.py`(LOG_LEVEL·LOG_FORMAT).

## 기록

호출마다 provenance 를 채운다: 실제 사용 모델·params(재시도로 바뀐 값 포함), 입력/출력/추론 토큰, 지연, 비용.
참고치(시험): 이력서 1건 주장 추출 106초, $0.005.

로그에는 메시지 본문을 남기지 않는다 — 프롬프트 id·해시·토큰 수만.

## 원문 인용 대조

주장 추출처럼 `원문` 필드를 돌려받는 작업은 `verify_quote(quote, source)` 를 거친다.
- 칸 표시는 **`⟦담당업무⟧` 형식만** 쓴다 — 가명화 토큰 `[ORG_1]` 과 겹치지 않게. 비교 전 양쪽에서 칸 표시를 지우고 **공백·줄바꿈을 모두 없앤다** (`llm/quotes.quote_in`).
  PDF·복사본은 단어 중간에서 줄이 끊기고 모델은 붙여 인용한다 — 공백을 하나로만 줄이던 예전 규칙은 이런 이력서에서
  주장을 절반까지 버렸다 (2026-11-04, 이력서 4개 × 3회: r3 26개 중 13개). 인용을 다시 찾는 곳
  (`derive_pseudonymized`, 칸 찾기 `locate_field`)도 같은 규칙을 쓴다 — 한 곳만 바꾸면 뒤에서 떨어진다.
  가명화 토큰은 원문의 일부라 지우지 않는다 (`llm/quotes.py`).
- 원문에 글자 그대로 없으면 그 항목은 폐기 + 에러 기록 (모델이 표시까지 인용하는 현상 대응).

## 테스트

- `FakeBackend` (`llm/fake.py`): 입력 해시별(없으면 default) 응답 큐. 예외도 넣을 수 있다. 네트워크 없이 그래프 전체 테스트.
- 실제 API 스모크: `uv run pytest -m llm -q -s` — 가상 텍스트만, 비용 발생, 기본 실행에서 제외.
- 재시도 경로(빈 응답 → low 재시도, 스키마 실패 → 재요청 → 실패)를 각각 테스트.
- 가명화되지 않은 입력 → 예외 테스트 (네거티브).
