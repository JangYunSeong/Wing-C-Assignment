# verify_overlap 승격 기록

④ 대조의 공고 역량 → 주장 확인 (top-down, `nodes/verify_overlap.py`). vN.md 는 평가한 그대로 고정한다 (prompt-registry §5).
평가 도구: `eval/retrieve/run_topdown.py` (주장 고정, 실행 2회). 결과는 git 밖 `data/eval/overlap/2026-11-04-topdown/`.

| 날짜 | 변경 | 모델 | 근거 | 승인 |
|---|---|---|---|---|
| 2026-11-04 | 첫 버전 CURRENT = **v1**, 운영 파이프라인 반영 (선택 경로와 규칙으로 합침) | opencode:qwen3.8-flash | 결정 실행 = 가상 test s09–s16 (1회만 열람, dev 결과로 프롬프트·코드 안 고침). 행 단위 action: 선택 경로 P 0.833 / R 0.636 → 확인 P 0.791·0.779 / R 0.964·0.964. 이력서×역량: 0.829/0.63 → 0.776/0.978. action+related 기준 확인 정밀도 0.97·0.94 (오탐 대부분 정답 related). 정답 none 의 action 비율 공고 안 0.016–0.023 · 대조군 0.022–0.065. 실행 간 action 일치 0.952. dev: 사용자 이력서 4개 0.85/0.459 → 0.805/0.892 · 0.72/0.973, 가상 s02–s08 0.84/0.808 → 0.797/0.981 · 0.765/1.0 | 사용자 2026-11-04 |

파일 해시 (sha256)
- 평가한 파일 (머리말 eval="평가 전용 실험…"): `220a33baf9d0ca5ec2632b649358866b61be1269c0e58d439f4dfdf0a1528b9b` — 평가 리포트 meta.prompt_sha 의 220a33baf9d0
- 운영 파일 (머리말 eval·changed 만 바꿈): `2f8106e5aacf7f4135215fb80b98a8d4e656482bdcacfd75d50db6e156012360` — 운영 provenance 의 prompt_sha256
- 본문 (두 번째 `---` 뒤, 지시문·규칙·예시): 두 파일 같음 `8d81a2f6399c6460774ed12f89ec4695a704dcc2f07ec603f912119368606e63`.
  평가 때 파일은 git 에 없어서, 지금 본문 + 평가 때 머리말로 다시 만들어 해시 220a33…b9b 가 그대로 나오는 것을 확인함 (2026-11-04).
  본문 확인: `python3 -c "import sys,hashlib;print(hashlib.sha256(open(sys.argv[1],encoding='utf-8').read().split('---\n',2)[2].encode()).hexdigest())" prompts/verify_overlap/v1.md`
- 프롬프트에 그려지는 출력 스키마(`VerifyOverlapOutput` 의 JSON 스키마)도 평가 때와 같다. 운영 파싱은 느슨한 모델
  (`VerifyParsedOutput` — 길이 한도 없음, label 은 문자열)로 받고 노드가 자르고 정리하지만, LLM 이 보는 스키마는 바꾸지 않았다.
  확인: `uv run python -c "import json,hashlib;from task_spec.schemas import VerifyOverlapOutput as V;print(hashlib.sha256(json.dumps(V.model_json_schema(),ensure_ascii=False,indent=1).encode()).hexdigest()[:16])"` → `ec9e7e6e257c857f`

주의
- 평가한 파일과 지금 파일은 머리말(eval·changed)만 다르다 — 위 해시 참고.
- 가상 이력서는 실제 이력서보다 선택 경로 재현율이 높게 나온다 (가상 0.64–0.81 vs 사용자 0.46). 확인 경로의 이득은 실제 이력서에서 더 컸다.
- 정밀도 손실은 주로 한 단계 위로 읽기 (LLM·RAG → 에이전트·자동화·AX, 배포 → 컨테이너·클라우드, 운영 → 서버 인프라). 승인 화면에서 승인자가 겹침을 관련 경험으로 내릴 수 있다 (`downgrade_overlap`).
- 목록 밖 id·빠진 id 가 호출 24번에 4–9건 — 노드가 빠진 역량만 한 번 다시 묻고, 그래도 빠지면 그 역량은 '확인 안 됨' (선택 경로만).
