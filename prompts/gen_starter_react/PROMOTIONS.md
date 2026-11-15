# gen_starter_react 승격 기록

vN.md 는 평가한 그대로 고정한다. 승격 결정과 근거는 이 파일에 남긴다 (prompt-registry §5). 프론트엔드 React 템플릿(frontend/react-vite-ts)이
manifest `prompt_tasks` 로 이 프롬프트를 쓴다 — Java 템플릿은 gen_starter 을 그대로 쓴다.

| 날짜 | 변경 | 모델 | 근거 | 승인 |
|---|---|---|---|---|
| 2026-11-09 | 첫 버전 CURRENT = **v1** | opencode:qwen3.8-flash (머리말) | 만들 때 CURRENT 로 둠 (생성기 CURRENT 는 최신 vN — 사용자 결정 2026-11-09) | React 템플릿 3단계 ("3단계 진행해줘") |
| 2026-11-10 | CURRENT v1 → **v2** (v2 sha256 b1f407b60bf2…) — 출력 상한 24000 → 16000 (지시문 동일) | opencode:qwen3.8-flash (머리말 — 알파는 gpt-4.1) | 오늘 알파(gpt-4.1, 낙관적 갱신 명세 samsung#006 재사용, 버전 고정): fe-v3-opt failed(계약 목록 출처 공백 → 계약 검사 추가) · fe-v3-opt-r2 review · fe-v3-main failed(허용 구현 상태 갱신 버그) · fe-v3-main-r2 review — 손 변형 사실 확인으로 숨은 테스트가 함정만 가림. 생성기 CURRENT 는 최신 vN (사용자 결정 2026-11-09) | 프론트 알파 조치 ("지금 멈추고 이거 수정해서 다시 진행해줄래?") |
| 2026-11-10 | CURRENT v2 → **v3** (v3 sha256 5a0075e32a96…) — 응답·데이터 타입은 src/api/contract-types.ts 의 <Api>Ok · 선택 목록은 OPTIONS | opencode:qwen3.8-flash (머리말) | 검증 알파 2026-11-10 (qwen, 새 판 고정, conform 작업 폴더): 프론트 6건 중 review 4 (목록 중복·토큰 갱신·로그아웃·낙관적 갱신 — 같은 입력의 이전 v6 실행은 1/6), 남은 2건(응답 순서·오프라인)은 겨냥한 실패(응답 모양 어긋남·선택 값 짐작)는 사라지고 ③ 변별 실패로 막힘. `data/eval/build/2026-11-10-alpha-cf{,2,3}-*` (conform 작업 폴더). 첫 실행의 토큰 갱신 실패는 정적 검사 오탐(655a353 로 고침), 낙관적 갱신·로그아웃 중단은 공급자 입력 거부(400) | 사용자 결정 2026-11-10 (계약 적합성 1단계 — '1,3 진행') |
| 2026-11-14 | CURRENT v3 → **v4** (v4 sha256 eaab9f03d11f…) — 의존성은 '기술 환경·package.json 에 있는 것만', 잠긴 파일은 입력의 템플릿 파일 목록대로 (이름 나열 삭제) | opencode:qwen3.8-flash (머리말) | 근거: #186 — 잠긴 파일 다섯·라이브러리 넷 나열이 {{template_files}} 입력과 겹치고 템플릿 판이 바뀌면 어긋난다 (gen_starter_py v3 전례). 첫 평가(React 알파 2건, qwen, 이전 명세 재사용 — 메인 체크아웃에서): `2026-11-14-alpha-g186b-retry-ae049` review 0회차 $0.0463 (같은 명세 gpt-4.1 review $0.2164) · `2026-11-15-alpha-g186b-offline-ae050` review 1회차 $0.0638 (같은 재료 gpt-4.1 fe8·fe9 실패). 요소 찾기 test id·textsByRole 만(공개 25·숨은 16곳, 이름·글·역할 쿼리·DOM 선택자 0), 연결 끊기는 수용 기준이 말한 pt1·pt2 에만. 시작 코드 기존 테스트 App.test.tsx 첫 답 TS1005 가 두 번 — 컴파일 루프가 고침. 생성기 CURRENT 는 최신 판 (불변식 7 예외) | 사용자 (2026-11-14 "스택 하나씩 진행해줘" — 마일스톤 2 #186) |
