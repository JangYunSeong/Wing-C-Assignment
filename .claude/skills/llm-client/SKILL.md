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
  `llm/registry.py` 가 provider 접두로 클라이언트를 고른다. 모델 설정은 머리말이 SSOT, 코드 기본값으로 덮지 않는다.
- OpenCode 쪽이 지원하지 않는 파라미터(예: `reasoning_effort`)는 provider 별 허용 목록으로 걸러 보내고, 걸러진 사실을 provenance 에 남긴다.
- 구조화 출력: `with_structured_output(PydanticModel)` 우선, 미지원 모델은 JSON 모드 + Pydantic 검증.
- 키는 `Settings` 가 `.env` 에서 로드. 키 값을 로그·provenance·예외 메시지·`/healthz` 에 넣지 않는다.
- 비용 계산용 단가는 `llm/pricing.py` 표 하나 (모르는 모델은 `cost_usd: null`).

## 재시도

| 증상 | 처리 |
|---|---|
| 빈 응답 + 출력 토큰이 추론에 소진 | `reasoning_effort=low` 로 1회 재시도 (시험 사실: `qwen3.8-flash` 는 긴 입력에서 기본 추론 설정이면 빈 응답) |
| 타임아웃·5xx·연결 오류 | 지수 백오프 최대 3회 |
| JSON 파싱·스키마 검증 실패 | 검증 에러 메시지를 붙여 1회 재요청. 그래도 실패면 `SchemaViolation` |
| 4xx(인증·요청 오류) | 재시도 없음 |

재시도 횟수·사유는 provenance `retries` 에 남긴다.

## 기록

호출마다 provenance 를 채운다: 실제 사용 모델·params(재시도로 바뀐 값 포함), 입력/출력/추론 토큰, 지연, 비용.
참고치(시험): 이력서 1건 주장 추출 106초, $0.005.

로그에는 메시지 본문을 남기지 않는다 — 프롬프트 id·해시·토큰 수만.

## 원문 인용 대조

주장 추출처럼 `원문` 필드를 돌려받는 작업은 `verify_quotes(output, source)` 를 거친다.
- 비교 전 양쪽에서 표시(`[담당업무]`, `[문제 정의]` 등 프롬프트가 붙인 칸 표시)와 공백 정규화를 제거.
- 원문에 글자 그대로 없으면 그 항목은 폐기 + 에러 기록 (모델이 표시까지 인용하는 현상 대응).

## 테스트

- `FakeLLM`: (prompt_id, 입력 해시) → 고정 응답 테이블. 네트워크 없이 그래프 전체 테스트.
- 재시도 경로(빈 응답 → low 재시도, 스키마 실패 → 재요청 → 실패)를 각각 테스트.
- 가명화되지 않은 입력 → 예외 테스트 (네거티브).
