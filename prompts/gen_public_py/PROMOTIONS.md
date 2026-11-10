# gen_public_py 승격 기록

vN.md 는 평가한 그대로 고정한다. 승격 결정과 근거는 이 파일에 남긴다 (prompt-registry §5). 데이터 엔지니어 템플릿(data/python-duckdb)이
manifest `prompt_tasks` 로 이 프롬프트를 쓴다 — Java·React 템플릿은 각자의 프롬프트를 그대로 쓴다.

| 날짜 | 변경 | 모델 | 근거 | 승인 |
|---|---|---|---|---|
| 2026-11-09 | 첫 버전 CURRENT = **v1** | opencode:qwen3.8-flash (머리말) | 만들 때 CURRENT 로 둠 (생성기 CURRENT 는 최신 vN — 사용자 결정 2026-11-09) | 데이터 엔지니어 진행 ("데이터 엔지니어 일단 추천대로 진행해줘") |
| 2026-11-09 | CURRENT v1 → **v2** | opencode:qwen3.8-flash (머리말) · 알파는 openai:gpt-4.1 | v1 알파 3회(kakaobank_2026#002): 공개 테스트가 `with workspace()`·`write_csv(header=)`·`partitions(폴더)`(key 빠짐)로 모든 구현에서 예외, 실행 날짜 05-28 한 번으로 05-27 출력까지 기대(하루 몫만 처리하는 흔한 구현이 공개 테스트에서 실패) → 하루 몫 원칙·하네스 시그니처 원칙 추가. 정적 검사(키워드·필수 인자·with)와 함께 | 생성기 CURRENT 는 최신 vN (사용자 결정 2026-11-09) |
