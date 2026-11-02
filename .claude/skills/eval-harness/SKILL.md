---
name: eval-harness
description: P1 평가 체계 — 가상 표본 생성(템플릿·LLM), dev/test 분리와 숨김 규칙, 평가 실행기, 리포트, 회귀 기준치, 프롬프트 CURRENT 승격 판단. 가명화 재현율 측정, 평가 표본 늘리기, 탐지기·프롬프트를 고친 뒤 수치 비교, held-out 평가, gold 데이터 작업을 할 때 사용. LLM 평가 일반론은 llm-evaluation 스킬을 함께 본다.
---

# eval-harness — 평가 체계

## 원칙

1. **dev 와 test 를 나눈다.** dev 는 고칠 때 보는 세트, test 는 숨긴다.
   - test 의 놓친 사례는 보지 않는다 (`run_eval --split test --show-misses` 는 거부된다).
   - test 수치가 나쁘면 test 를 보고 고치지 말고, **새 dev 표본**을 만들어 거기서 패턴을 찾는다.
   - test 는 사람이 정한 시점(PR 전 등)에만 돌리고 리포트로 남긴다.
2. **표본은 전부 가상.** 실존 인물·연락처를 쓰지 않는다. 전화 `010-0000-XXXX`, 이메일 `example.*`, 주소 `가상로`.
3. **정답은 코드가 심은 값**(planted). 사람이 라벨링하지 않아도 재현율을 잴 수 있게 한다.
4. **설계상 못 잡는 값(hard)** 은 따로 집계한다 — 한계를 수치로 보이게.
5. **템플릿 수치는 낙관적이다.** dev·test 가 같은 레이아웃을 쓰므로 일반화는 LLM 작성 표본 수치로 판단한다.

## 가명화 평가 (`eval/pseudonymize/`)

| 파일 | 역할 |
|---|---|
| `generate_template.py` | 시드 고정 템플릿 표본 (레이아웃 4종: labeled / header_slash / english / cover_prose). 재생성 가능 |
| `generate_llm.py` + `prompts/generate_v1.md` | LLM(gpt-4.1-mini)이 쓴 문체 다양 표본. 비결정적이라 결과 JSONL 을 커밋해 고정 |
| `samples/*_{dev,test}*.jsonl` | `{id, split, source, layout, text, planted[{category,value}], keep[], hard[]}` |
| `run_eval.py` | 범주별 재현율, 과잉 치환(keep 소실), 누락 검사로 멈춘 문서 수, hard 재현율 |
| `reports/<date>_<split>.{md,json}` | 실행 기록 (`--save`) |

```bash
uv run python -m eval.pseudonymize.generate_template --n 100 --seed 1 --split dev
uv run python -m eval.pseudonymize.generate_llm --n 20 --seed 11 --split dev    # 비용 발생, 가상 입력만
uv run python -m eval.pseudonymize.run_eval --split dev --show-misses
uv run python -m eval.pseudonymize.run_eval --split test --save                 # 숨김 세트, 수치만
```

- 표본을 늘릴 때는 **새 시드·새 파일**로 추가한다 (기존 파일 덮어쓰기 금지 — 과거 수치와 비교가 깨진다).
- 회귀 기준치: `tests/eval/test_pseudonymize_eval.py` (dev 하한). 탐지기를 고치면 이 테스트가 통과해야 한다.

## 기록 (2026-11-02)

| 세트 | 전체 | PERSON | ORG | PRODUCT | 과잉 치환 | hard |
|---|---|---|---|---|---|---|
| dev 119건 | 98.4% | 100% | 98.0% | 60.0% | 0% | 0/20 |
| test 120건 (숨김) | 97.5% (LLM 86.6%) | 98.9% | 93.4% | 51.3% | 0% | 0/14 |

다음: 사전·접미어 없는 회사(hard 0%) — 경력 줄 구조(기간과 붙은 이름) 규칙. test PERSON 누락 3건은 새 dev 표본으로 원인 탐색.

## 주장 추출 평가 [예정]

`extract_claims` 프롬프트 승격(prompt-registry §5)에 쓴다. gold = 가상 지원서의 행동 단위 주장. 지표: 주장 재현율·정밀도,
인용 일치율, 반복 실행 안정성, 모델 간 비교(qwen3.8-flash vs gpt-4.1-mini). 구조는 위와 같게(dev/test, 리포트, 회귀).
