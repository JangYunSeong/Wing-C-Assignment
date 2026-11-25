# gen_starter_py 승격 기록

vN.md 는 평가한 그대로 고정한다. 승격 결정과 근거는 이 파일에 남긴다 (prompt-registry §5). 데이터 엔지니어 템플릿(data/python-duckdb)이
manifest `prompt_tasks` 로 이 프롬프트를 쓴다 — Java·React 템플릿은 각자의 프롬프트를 그대로 쓴다.

| 날짜 | 변경 | 모델 | 근거 | 승인 |
|---|---|---|---|---|
| 2026-11-09 | 첫 버전 CURRENT = **v1** | opencode:qwen3.8-flash (머리말) | 만들 때 CURRENT 로 둠 (생성기 CURRENT 는 최신 vN — 사용자 결정 2026-11-09) | 데이터 엔지니어 진행 ("데이터 엔지니어 일단 추천대로 진행해줘") |
| 2026-11-09 | CURRENT v1 → **v2** | opencode:qwen3.8-flash (머리말) · 알파는 openai:gpt-4.1 | 알파 4회차: 참조 구현이 예외를 삼키고 종료 코드만 내 (출력 폴더를 만들지 않아 실패) 수리가 원인을 못 봄 → '실패는 보이게'(stderr 한 줄·출력 폴더 만들기) 추가. manifest repair_terms.impl_crash 가 그 줄을 보고 그 구현을 다시 만든다 | 생성기 CURRENT 는 최신 vN (사용자 결정 2026-11-09) |
| 2026-11-09 | CURRENT v2 → **v3** | opencode:qwen3.8-flash (머리말) · 알파는 openai:gpt-4.1 | 템플릿에 pandas 2.3·numpy 2.4 를 넣어 ('numpy,pandas 넣어야겠는데?') v2 의 의존성 문장(DuckDB·pyarrow 만)이 어긋남 → 라이브러리 이름 대신 '기술 환경·pyproject.toml 에 있는 것'으로 | 생성기 CURRENT 는 최신 vN (사용자 결정 2026-11-09) |
| 2026-11-14 | CURRENT v3 → **v4** (v4 sha256 6f8c1f4ad83b…) — '출력 폴더(와 그 상위)는 없을 수 있으니 만든다' → '옵션으로 받은 경로가 미리 있다고 기대하지 않음 (계약·입력 환경이 있다고 정한 것만)' | opencode:qwen3.8-flash (머리말) | 근거: #187 — 2026-11-09 데이터 알파 4회차 크래시 원인 하나를 덧붙인 줄 (세 생성기 같은 문장). 첫 평가: 데이터 알파 2건 (평가 전). 생성기 CURRENT 는 최신 판 (불변식 7 예외) | 사용자 (2026-11-14 "스택 하나씩 진행해줘" — 마일스톤 2 #187) |
| 2026-11-24 | CURRENT v4 → **v5** (v5 sha256 a58bc08e19a0…) — '새 기능 테스트는 쓰지 않음 — 새 명령을 실제 옵션으로 실행하거나 자리 표시의 미구현 상태(NotImplementedError·실패 종료 코드)를 단언하지 않음, 새 명령은 --help 까지만' | opencode:qwen3.8-flash (머리말) | 근거: #256 5단계 재료 C 알파 — 시작 코드 테스트가 '옵션을 주면 종료 코드 1 · stderr 에 NotImplementedError' 를 단언해 참조 구현이 모두 깨고 빌드가 끝났다 (저장된 데이터 시작 코드 32개 중 3개 — 11-15 g187c · 11-24 재료 C 2회, 셋 다 빌드 실패). Java·React 판은 v1 부터 이 문장이 있었고 Python 판은 v1 부터 빠졌다. 같은 꼴은 정적 검사(`static_check._starter_stub_tests`)가 생성 직후 되돌린다. 첫 평가: 재료 C·batch-rerun 알파 각 3회 (평가 전). 생성기 CURRENT 는 최신 판 (불변식 7 예외) | 사용자 (2026-11-24 "A+C로 먼저 진행하고 B 진행하자") |
