---
name: prompt-registry
description: 프롬프트 버전 관리·조립·provenance 규칙. prompts/<task>/vN.md + CURRENT 파일 형식, 머리말(모델·설정·스키마·평가 결과·변경 이유), LLM 호출마다 지시문·판단 기준·few-shot 예시·입력 4요소 조립, 예시 검색과 정답 누설 방지, provenance 기록, 평가 후 CURRENT 승격 절차를 다룬다. 프롬프트를 새로 쓰거나 고칠 때, LLM 을 부르는 노드를 만들 때, 프롬프트 평가·승격을 할 때 사용.
---

# prompt-registry — 프롬프트 관리

출처: 온톨로지 레포 #59 댓글(2026-10-30) "프롬프트 관리 단계가 필요하다".
문제: 프롬프트가 스크립트 문자열이면 **어느 결과가 어느 프롬프트에서 나왔는지 추적할 수 없고, 고친 뒤 비교할 수 없다.**

## 1. 파일 구조

```
prompts/
  extract_claims/
    v1.md
    v2.md
    CURRENT          # 한 줄: v2
  select_competency/
    ...
```

- **기존 vN.md 는 수정하지 않는다** (훅 `protect-prompt-versions.sh` 가 막는다). 오탈자도 새 버전.
- `CURRENT` 는 평가 통과 후에만 바꾼다 (§5).

### vN.md 형식

```markdown
---
id: extract_claims
version: 2
model: opencode:qwen3.8-flash      # <provider>:<model> (llm-client)
params: {temperature: 0, reasoning_effort: low, max_tokens: 4096}
input_schema: ClaimExtractionInput      # src/task_spec/schemas 의 클래스명
output_schema: ClaimExtractionOutput
criteria: [spec-decisions-claims]       # 판단 기준 출처 키 (§2)
fewshot: {source: judgments, k: 2, match: [sub_family, industry]}
eval:                                   # 승격 판단 기록
  set: claims-gold-v1
  precision: 0.91
  recall: 0.84
  stability: 0.95
  heldout: true
changed: "v1 대비: 과정 칸 문장을 '참여'로 쓰라는 규칙을 지시문에서 제거 (역할은 규칙 판정으로 이관)"
---

## system
역할·출력 JSON 형식 지시문

## user
{{criteria}}

{{examples}}

{{input}}
```

## 2. 조립 — 네 요소

| 요소 | 출처 | 바뀌는 때 |
|---|---|---|
| 지시문 | `prompts/<task>/vN.md` | 프롬프트를 고칠 때 |
| 판단 기준 | 온톨로지 레포가 export 한 결정 기록 문장 (`data/review/spec/spec-decisions-*.md` → 산출물에 포함) | 판정을 다시 할 때 |
| 예시 (few-shot) | **판정 데이터에서 검색** — 같은 직군·업종 판정 사례 1~2개 | 판정이 늘 때 |
| 입력 | 가명화된 지원자 데이터 / 온톨로지 산출물 | 실행마다 |

```python
assembled = prompting.assemble(
    task="extract_claims",
    input=payload,                 # Pydantic 모델. 가명화 타입만 허용 (PseudonymizedText)
    context=FewshotContext(sub_family=..., industry=..., exclude_positions={eval_position_id}),
)
# assembled.messages, assembled.params, assembled.provenance
```

- 기준·예시를 말로 풀어 쓰기보다 **판정 사례를 예시로** 넣는 쪽을 우선한다
  (주 역량 선택 시험: "자격요건에만 나왔지만 담당업무가 전제하는 역량" 재현율 68%).
- **정답 누설 금지**: 평가 중에는 평가 대상 포지션의 판정 사례를 예시로 가져오지 않는다.
  `exclude_positions` 를 비우고 평가를 돌리면 assemble 이 예외를 던진다(`mode="eval"`).
- 입력 타입이 가명화 타입이 아니면 assemble 이 거부한다 (가명화 불변식의 두 번째 방어선).
- 모델이 프롬프트의 표시(`[담당업무]` 등)까지 인용하므로, 표시는 인용 검사에서 떼어낼 수 있는 고정 형식으로만 쓴다.

## 3. provenance

LLM 호출 1건마다:

```json
{
  "prompt": "extract_claims@2", "prompt_sha256": "…",
  "model": "opencode:qwen3.8-flash", "params": {"temperature": 0, "reasoning_effort": "low"},
  "fewshot_ids": ["judg:…", "judg:…"], "criteria_ids": ["spec-decisions-claims#3"],
  "ontology_version": "onto-v2026.10.30",
  "retries": 1, "tokens": {"in": 0, "out": 0, "reasoning": 0}, "latency_ms": 0, "cost_usd": 0
}
```

- 해시는 **조립 전 템플릿 파일** 기준 (입력이 다르면 달라지는 값은 해시에 넣지 않는다).
- 명세의 각 항목은 자신을 만든 provenance 를 id 로 가리킨다 → 승인자가 "왜 이 역량이 주인지" 따라갈 수 있어야 한다.

## 4. 코드 규칙

- 코드에 프롬프트 문자열을 쓰지 않는다. 로더는 `CURRENT` 를 읽고, 테스트·평가는 버전을 명시할 수 있다.
- 템플릿 변수는 `{{name}}` 만. 누락·남는 변수는 로드 시 에러.
- 로더는 시작 시 모든 `CURRENT` 를 검증(머리말 스키마, 스키마 클래스 존재).

## 5. 승격 절차 (vN → CURRENT)

1. 새 `vN+1.md` 추가 (머리말 `changed` 필수).
2. 평가셋으로 실행: 현재 주 선택 13포지션, 자기소개서·이력서 1건 + **프롬프트를 고칠 때 보지 않은 표본(heldout)**.
3. 정밀도·재현율·안정성(같은 입력 반복·판본 차이 간 일치도) 이 **현재 CURRENT 보다 나빠지지 않을 때만** 승격.
4. 결과를 vN+1 머리말 `eval` 에 적고, `CURRENT` 를 바꾸는 커밋을 따로 만든다.

`prompt-engineer` 에이전트는 1~3 까지 하고 승격은 제안만 한다. `CURRENT` 변경은 사람이 승인.

## 6. 경계

프롬프트·조립·평가는 이 레포(P1). 온톨로지 레포는 재료(판정 사례 export, 판단 기준 문장)만 내보낸다.
외부 프롬프트 관리 도구는 쓰지 않는다 (파일 + git).
