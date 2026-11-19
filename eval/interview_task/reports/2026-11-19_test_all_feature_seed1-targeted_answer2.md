# D2 새 프롬프트 평가 — test (2026-11-19)

- 프롬프트: interview_feature_seed@1, interview_targeted_answer@2
- 표본: 기능형 재료 6건 · 답할 범위 초안 6건 · 판정 **통과**

| 기준 | 값 | 규칙 | 결과 |
|---|---|---|---|
| seed_valid | 0.833 | >= 0.8 | 통과 |
| seed_reason_hit | 1.0 | >= 0.8 | 통과 |
| seed_concept_hit | 1.0 | >= 0.8 | 통과 |
| seed_harness_ok | 1.0 | >= 0.8 | 통과 |
| seed_need_leak | 0 | <= 0 | 통과 |
| seed_designs_ok | 1.0 | >= 0.8 | 통과 |
| seed_blocked | 0 | <= 0 | 통과 |
| answer_valid | 1.0 | >= 0.9 | 통과 |
| answer_solution_leak | 0 | <= 0 | 통과 |
| answer_artifact_id | 0 | <= 1 | 통과 |
| answer_invented_numbers | 0 | <= 1 | 통과 |
| answer_too_long | 0 | <= 1 | 통과 |
| answer_pointer_hit | 1.0 | >= 0.6 | 통과 |

참고: 호출 평균 27.3초 · 토큰 {'in': 32500, 'out': 19746, 'reasoning': 14833} · 비용 $0.0142

| 재료 표본 | 통과 | 사유 | 개념 | harness | 목적 누설 | 인정 설계 | 등록 막힘 | 초 |
|---|---|---|---|---|---|---|---|---|
| seed-test-01 | O | O | O | O | 0 | 2 | 0 | 37.6 |
| seed-test-02 | O | O | O | O | 0 | 2 | 0 | 66.9 |
| seed-test-03 | O | O | O | O | 0 | 2 | 0 | 59.6 |
| seed-test-04 | X | X | X | X | 0 | 0 | 0 | 34.0 |
| seed-test-05 | O | O | O | O | 0 | 2 | 0 | 56.2 |
| seed-test-06 | O | O | O | O | 0 | 2 | 0 | 34.8 |

| 초안 표본 | 통과 | 해법 누설 | 자료 id | 지어낸 수치 | 길이 | 자료 가리킴 | 초 |
|---|---|---|---|---|---|---|---|
| ans-test-01 | O | 0 | - | 0 | 106 | O | 7.8 |
| ans-test-02 | O | 0 | - | 0 | 157 | O | 9.6 |
| ans-test-03 | O | 0 | - | 0 | 127 | O | 6.9 |
| ans-test-04 | O | 0 | - | 0 | 110 | O | 7.5 |
| ans-test-05 | O | 0 | - | 0 | 135 | O | 7.0 |
| ans-test-06 | O | 0 | - | 0 | 104 | O | 6.9 |
