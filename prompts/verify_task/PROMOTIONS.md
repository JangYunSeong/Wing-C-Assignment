# verify_task 승격 기록

vN.md 는 평가한 그대로 고정한다 (prompt-registry §5). 평가: `eval/spec/run_verify.py` — 시범 공고 직군별 1건(dev 6 / test 6),
공고마다 공통 과제(draft_task CURRENT) + 가상 주장 3건 → 확장 문항. 리포트는 `data/eval/spec/` (git 밖).

| 날짜 | 변경 | 모델 | 근거 | 승인 |
|---|---|---|---|---|
| 2026-11-03 | 첫 버전 CURRENT = **v1** | opencode:qwen3.8-flash (temperature 0.3, reasoning_effort low) | dev 11문항: 재요청 후 검증 11/11, 첫 답 7/11. test 11문항: 재요청 후 10/11, 첫 답 9/11, 원문 베끼기 0, 면접 질문 3개/문항, $0.027. 실패 1건(data.eng — 핵심 개념 'shuffle' 노출 → review_flags) | 사용자 확인 (2026-11-03, 확장 문항 방식 ①) |

주의
- 가상 주장은 문장 틀 하나에서 만든 것이라 실제 지원서 문체에서 같다는 보장은 없다.
- 모델 비교·LLM 판정은 하지 않았다 (공통 과제와 같은 모델로 시작). 품질 근거는 자동 검증 + 샘플 검토뿐 — 타당도(경험자 변별)는 미측정.

## 폐기 (2026-11-03)
지원자마다 확장 문항을 서술하던 이 방식은 **공고 단위 확장 슬롯 + 지원자별 배정·면접 질문**으로 대체했다
(`prompts/draft_slot`, `prompts/slot_questions`, `spec/slots.py`·`assign.py`·`questions.py`). 근거: AI 단독 풀이 테스트에서
경험 없는 AI 도 서면 확장 문항 22개 모두 수준 3 이상 — 서면 문항으로는 경험을 가리지 못했고, 지원자마다 문항을 생성·검증하는
비용·지연도 컸다. 프롬프트 파일과 입출력 스키마는 기록용으로 남긴다 (기동 시 CURRENT 검증 대상). 코드에서는 쓰지 않는다.
