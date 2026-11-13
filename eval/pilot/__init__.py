"""손 시범 도구 (docs/design/analysis-stage.md §9.1, 2026-11-13 확정) — 평가 전용.

물어서 요구를 알게 하는 기능형 1단계가 판별력을 만드는지, 큰 구현 전에 손으로 만든 재료로 확인한다.

- model       재료(사실 두 층·요구별 예측·공개 수용 기준 → 요구)와 풀이 판, 경로
- facilitator 진행자 서버와 작업 폴더의 `./ask` — 답은 starter/answer_rule.py 그대로
- prepare     판별 작업 폴더 (레포 밖) 와 풀이 지시문
- grade       공개 테스트 ①(원래)·②(행동만)·숨은 테스트를 메서드 단위로
- analyze     §9.1 판정 (재료별 조건, 평균 효과, 적중률, 원인 가르기) 과 기록 점검
- freeze      판 0 전에 한 번 — 해시 목록 (파일 + 설계 문서·도구 커밋)
- expand_terms 진행자 쪽 고치기의 동의어 목록 (고정 절차 — prompts/expand_match_terms)

데이터는 git 밖 `data/eval/analysis-pilot/` (공고로 만든 과제와 AI 풀이 — 지원자 데이터 없음).
작업 폴더는 레포 밖 `/ssd/solo_runs/analysis-pilot/` — 풀이가 레포·숨은 테스트·사실 파일을 볼 수 없게.
"""
