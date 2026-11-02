# select_competency 승격 기록

vN.md 는 평가한 그대로 고정한다. 승격 결정과 근거는 이 파일에 남긴다 (prompt-registry §5). 리포트: `eval/retrieve/reports/`.

| 날짜 | 변경 | 모델 | 근거 (test, 숨김 세트) | 승인 |
|---|---|---|---|---|
| 2026-11-02 | 첫 버전 CURRENT = **v1** | opencode:qwen3.8-flash | 검색(hybrid, 범위 soft ×0.7) 상위 10 → 선택. gold 검토 반영본(accept·kind) test 20건: 정밀도 0.905, 재현율 0.95, 완전 일치 0.85, 반복 안정성 0.9. gpt-4.1-mini 는 정밀도 0.76·완전 일치 0.65 (역량을 2개씩 더 고르는 경향). 약점: 정답 없음(g09·g10)에도 ai.dl·ai.service 를 고름, multi(g34)에서 하나만 고름 | 대기 |

주의: gold 는 가상 주장 40건(초안, 사용자 검토 중)이라 수치가 실제 지원서 문체에서 같다는 보장은 없다.
test 세트는 이번에 한 번 열었으므로, 다음 프롬프트 개선(v2, uncovered 판단)은 gold v2 의 새 항목으로 평가한다.
