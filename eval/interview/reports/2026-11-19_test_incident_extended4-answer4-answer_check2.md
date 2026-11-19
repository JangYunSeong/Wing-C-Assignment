# 사례 인터뷰 프롬프트 평가 — test (2026-11-19)

- 프롬프트: interview_incident_extended@4, interview_answer@4, interview_answer_check@2
- 모델: opencode:qwen3.8-flash (프롬프트 머리말)
- 표본 5건 · 판정 **미달**

| 기준 | 값 | 규칙 | 결과 |
|---|---|---|---|
| completion | 0.8 | >= 0.8 | 통과 |
| reasks | 0 | <= 1 | 통과 |
| leading | 1 | <= 0 | 미달 |
| unavailable_ratio | 0.107 | <= 0.2 | 통과 |

참고: targeted 0.909 · 사례 대화 평균 2.2턴 · 보충 질문 1번 · LLM 단계 평균 32.9초 (p90 88.5초) · 토큰 {'in': 87717, 'out': 65021, 'reasoning': 41826} · 비용 $0.0437

| 표본 | 완성 | 끝 단계 | 대화 턴 | 재질문 | 유도 | 503 | 보충 질문 | 검증 |
|---|---|---|---|---|---|---|---|---|
| test-01-stock-lost-update | O | competencies | 2 | 0 | 0 | 0/5 | 0 | True |
| test-02-token-refresh | O | competencies | 3 | 0 | 0 | 0/6 | 0 | True |
| test-03-timezone-boundary | X | trap_interview | 2 | 0 | 1 | 3/6 | 0 | None |
| test-04-pool-exhaustion | O | competencies | 2 | 0 | 0 | 0/5 | 0 | True |
| test-05-pagination-drift | O | competencies | 2 | 0 | 0 | 0/6 | 1 | True |
