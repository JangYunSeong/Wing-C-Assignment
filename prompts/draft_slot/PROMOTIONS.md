# draft_slot 승격 기록

vN.md 는 평가한 그대로 고정한다 (prompt-registry §5). 평가: `eval/spec/run_slots.py` — draft_task v3 로 만든 공통 과제(dev 6 공고) 위에 슬롯 생성.

| 날짜 | 변경 | 모델 | 근거 | 승인 |
|---|---|---|---|---|
| 2026-11-03 | 첫 버전 CURRENT = **v1** | opencode:qwen3.8-flash (temperature 0.3, reasoning_effort low) | dev 6 공고: 슬롯 21개(공고당 평균 3.5, 근거 역량이 2개뿐인 data.ml 은 2개), 생성 실패 0, 첫 답 자동 검증 실패 4 → 재요청 후 1(review_flags), 공통 과제 역량과 같은 역량 슬롯 9개(공고 밖 역량이 적을 때 다른 각도), $0.11, 공고당 최대 약 4분 | 사용자 확인 (2026-11-03, 확장 슬롯 방식) |

주의
- 자동 검사로는 "공통 과제를 풀지 않아도 풀 수 있는가", "슬롯끼리 너무 비슷하지 않은가"를 보지 못한다 — 승인자 검토와 샘플 검토(data/eval/spec/*-slots-*/samples.md).
- 슬롯 단위 AI 단독 풀이 테스트·판정 비교는 아직 하지 않았다.
- test 세트 평가는 아직 하지 않았다.
