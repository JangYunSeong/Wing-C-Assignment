# D2 새 프롬프트 평가 — dev (2026-11-19)

- 프롬프트: interview_feature_seed@1, interview_targeted_answer@2
- 표본: 기능형 재료 6건 · 답할 범위 초안 6건 · 판정 **통과**

| 기준 | 값 | 규칙 | 결과 |
|---|---|---|---|
| seed_valid | 1.0 | >= 0.8 | 통과 |
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

참고: 호출 평균 27.8초 · 토큰 {'in': 38785, 'out': 22592, 'reasoning': 17538} · 비용 $0.0164

| 재료 표본 | 통과 | 사유 | 개념 | harness | 목적 누설 | 인정 설계 | 등록 막힘 | 초 |
|---|---|---|---|---|---|---|---|---|
| seed-dev-01 | O | O | O | O | 0 | 2 | 0 | 32.2 |
| seed-dev-02 | O | O | O | O | 0 | 2 | 0 | 59.1 |
| seed-dev-03 | O | O | O | O | 0 | 2 | 0 | 31.7 |
| seed-dev-04 | O | O | O | O | 0 | 2 | 0 | 33.1 |
| seed-dev-05 | O | O | O | O | 0 | 2 | 0 | 50.2 |
| seed-dev-06 | O | O | O | O | 0 | 2 | 0 | 63.8 |

| 초안 표본 | 통과 | 해법 누설 | 자료 id | 지어낸 수치 | 길이 | 자료 가리킴 | 초 |
|---|---|---|---|---|---|---|---|
| ans-dev-01 | O | 0 | - | 0 | 112 | O | 7.7 |
| ans-dev-02 | O | 0 | - | 0 | 97 | O | 9.4 |
| ans-dev-03 | O | 0 | - | 0 | 113 | O | 7.8 |
| ans-dev-04 | O | 0 | - | 0 | 164 | O | 15.8 |
| ans-dev-05 | O | 0 | - | 0 | 159 | O | 14.9 |
| ans-dev-06 | O | 0 | - | 0 | 85 | O | 8.0 |
