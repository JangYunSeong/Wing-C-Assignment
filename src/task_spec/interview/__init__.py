"""채용담당자 사례 인터뷰 (#262).

Wing-C-Interview 포크(9ef2f50)의 대화 흐름을 옮겼다. D1: 기존 공고에서 시작 → 팀 맥락 → 사례 대화 → 처음 시도한 대응
→ 성공 기준·기준 답안·답안 검증 → 사례·답안 확인. 역량·과제·승인(interrupt 그래프)·회사 사례 등록은 D2.

- 설계: docs/design/recruiter-interview.md (결정·이식 계획).
- 대화는 API + 형 있는 상태(schemas/interview_state.py). 승인만 그래프 (불변식 9 — D2).
- 담당자 글은 nodes/pseudonymize/interview.py 가 가명화한 뒤에만 LLM 으로 간다 (불변식 2, 결정 4).
- 프롬프트(interview_incident_extended·interview_answer·interview_answer_check)에 CURRENT 가 없으면 '준비 중'(503).
"""
