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

## 추출 평가 (`eval/ingest/`)

`generate.py`(시드 고정, 레이아웃 7종 — 파일은 평가 때 임시 폴더에 생성, 커밋하지 않음) + `run_eval.py`.
지표: 본문 일치·칸 재현율·칸 순서 정확·잘못 만든 칸·기간 인식·2단 순서. 기록은 document-ingest 스킬.

```bash
uv run python -m eval.ingest.run_eval --split dev --show-misses
uv run python -m eval.ingest.run_eval --split test --save
```

## 새 공고 평가 (`eval/postings/`)

온톨로지 시범 직군 공고 239건을 '새 공고처럼' 넣고 검수 결과와 비교 (dev/test = 포지션 id 해시).
원문 재구성: 절 제목 표기 3종 + 제목 없음 15% (시드 고정). 입력은 온톨로지 클론 `data/ontology/src/data/postings/parsed/` (git 밖).
지표: 절 분리, 직군 top-1/3(평가 대상 자신은 직무명 비교에서 제외), 업종(등록 회사 / 모르는 회사 LLM top-1·3),
역량 재현율·정밀도(정답 직군 / 추천 1위 직군), LLM 보조 실패 수.

```bash
uv run python -m eval.postings.run_eval --split dev --llm --unknown-sample 40 --show-misses
uv run python -m eval.postings.run_eval --split test --llm --unknown-sample 40 --suggest-version 2 --save
```

| 2026-11-02 test (숨김, 114) | 규칙만(dev 참고) | LLM 보조, suggest_field v2 |
|---|---|---|
| 절 분리 | 0.754 | 0.99 |
| 직군 top-1 / top-3 | 0.79 / 0.919 | 0.886 / 0.965 |
| 업종 (등록 / 모르는 회사 top-1 / top-3) | 0.919 / - / - | 0.939 / 0.725 / 0.85 |
| 역량 재현율·정밀도 (추천 직군) | 0.742 / 0.988 | 0.987 / 0.984 |

절 분리는 표기를 재구성한 것이라 낙관적. 직군·역량은 실제 공고 문장 기준. suggest_field v1→v2 승격 기록: `prompts/suggest_field/PROMOTIONS.md`.

## 사례 인터뷰 평가 (`eval/interview/`, #263)

가상 대화 표본(`samples.py`, dev 5 · test 5 — 백엔드·프런트엔드·데이터·인프라, 고정 질문·'모름'·처음 대응 모름·가명화 토큰 포함)마다
담당자 대본이 실제 `InterviewService`(탐지기 가명화·저장 전 검사 포함)를 D1 흐름 끝까지 돈다. 실제 LLM 호출(비용 발생, 한 세트 약 $0.05).
담당자는 질문 낱말로 묻는 칸을 짐작해 그 칸의 사실로 답한다. 정답은 표본에 심은 값.
지표: 완성률 · 재질문(답한 칸을 다시 '빠짐'으로 봄) · 유도(담당자가 말하기 전 원인 낱말 — 낱말 일치라 오탐 가능) · 503 비율 · targeted(참고) · 지연·토큰·비용.
통과 기준은 `run_eval.CRITERIA`(실행 전 고정). 채점기 자기 검증: `tests/eval/test_interview_eval.py`.

```bash
# .env 가 있는 레포 루트에서. 워크트리면: PYTHONPATH=<워크트리> uv run --project <워크트리> python <워크트리>/eval/interview/run_eval.py …
uv run python -m eval.interview.run_eval --split dev --show-transcripts --save
uv run python -m eval.interview.run_eval --split test --save      # 숨김 — 대화 내용·실패 사유를 보지 않는다
```

| 2026-11-19 qwen3.8-flash · 포크 판(incident v4 · answer v4 · check v2) | dev 1회 | dev 2회 | test (숨김) |
|---|---|---|---|
| 완성 (≥ 0.8) | 1.0 | 1.0 | 0.8 |
| 재질문 (≤ 1) | 0 | 0 | 0 |
| 유도 (0) | 0 | 0 | 1 — 사용자 결정으로 판정 제외 |
| 503 비율 (≤ 0.2) | 0.103 | 0.1 | 0.107 |
| LLM 단계 평균 · p90 | 29.7초 · 59.8초 | 40.5초 · 94.0초 | 32.9초 · 88.5초 |

CURRENT 승격(첫 판) 근거 — 각 `prompts/interview_*/PROMOTIONS.md`. dev 1회 리포트의 입력 토큰 0 은 집계 버그(provenance 키 `in_`), 2회부터 고침.
다음: test-03(처음 대응 단계 503 3번으로 미완)의 원인을 새 dev 표본으로 — 한글 숫자 표기('아홉 시간')와 서버 숫자 검사(숫자만 셈)의 어긋남이 의심.

## 주장 추출 평가 (`eval/claims/`)

`generate.py`: 가명화 끝난 형태(토큰·⟦칸⟧)의 가상 이력서·자기소개서, 문장마다 정답(행동 수·칸·역할, 뽑으면 안 되는 문장).
`run_eval.py`: 실제 LLM 호출(비용 발생). 모델 출력 인용 → 가장 가까운 정답 문장에 대응.
지표: 문장·행동 재현율, 두 행동 분리율, 정밀도, 행동 아닌 문장 비율, 인용 일치, 칸·역할 정확도, 2회 안정성, 지연·추론 토큰.
채점기 자기 검증(정답 입력 = 만점): `tests/eval/test_claims_scoring.py`.

```bash
uv run python -m eval.claims.run_eval --split dev --prompt-version 2 --show-misses --save
uv run python -m eval.claims.run_eval --split test --prompt-version 2 --save
```

| 2026-11-02 test (숨김) | qwen3.8-flash v1 → v2 | gpt-4.1-mini v1 → v2 |
|---|---|---|
| 행동 재현율 | 0.971 → 1.0 | 0.691 → 1.0 |
| 두 행동 분리율 | 1.0 → 1.0 | 0.071 → 1.0 |
| 안정성 | 0.988 → 1.0 | 0.973 → 1.0 |
| 지연 | 16.2s → 10.3s | 2.1s → 2.7s |

v2 를 CURRENT 로 승격(모델 qwen 유지). 한계: 템플릿 생성기라 dev·test 문장 풀이 같다 — 다음은 LLM 이 쓴 문체 다양 표본(가명화 평가처럼).
