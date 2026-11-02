# .claude/ 카탈로그 — 스킬 · 에이전트 · 훅

> 실측 기준일: 2026-11-02. 갱신: `ls .claude/skills | wc -l ; ls .claude/agents | wc -l ; ls .claude/hooks | wc -l`
> vendored 스킬은 `skills-lock.json` 에 출처·해시가 고정되어 있다 (`npx skills experimental_install` 로 복원).

## 스킬 — 23개 (자체 8 + vendored 15)

### 자체 (P0)

| 스킬 | 내용 |
|---|---|
| `pipeline-graph` | ①~④ LangGraph 그래프·State·승인 interrupt·에러 정책 SSOT |
| `fastapi-service` | API 엔드포인트 계약·백그라운드 실행·lifespan |
| `prompt-registry` | `prompts/<task>/vN.md` + `CURRENT`, 4요소 조립, provenance, 승격 절차 |
| `llm-client` | 모델 호출 래퍼·재시도(reasoning_effort=low)·인용 대조·FakeLLM |
| `ontology-artifact` | 버전 고정 산출물 읽기·테이블·셀 범위·NCS/ESCO 조인 |
| `pseudonymization` | 치환 대상·일관 토큰·누락 검사·`PseudonymizedText` |
| `document-ingest` | PDF(pypdfium2)·스캔(tesseract)·DOCX·HWPX 추출, 레이아웃 복원, ⟦칸⟧ 구조화 |
| `eval-harness` | 가상 표본(템플릿·LLM)·dev/test 숨김 규칙·평가 실행기·회귀 기준치 |

### 자체 — 예정

| 단계 | 스킬 |
|---|---|
| P1 | `claim-extraction`, `competency-retrieve`(hybrid-search 위에) |
| P2 | `cross-doc-link`, `spec-generation` |

### vendored (15)

| 출처 | 스킬 |
|---|---|
| `langchain-ai/langchain-skills` | langgraph-fundamentals · langgraph-persistence · langgraph-human-in-the-loop · langgraph-cli · langchain-fundamentals · langchain-rag · langchain-dependencies |
| `fastapi/fastapi` | fastapi |
| `wshobson/agents` | async-python-patterns · python-testing-patterns · hybrid-search-implementation · llm-evaluation |
| `astral-sh/claude-code-plugins` | uv |
| `anthropics/skills` | pdf |
| `acaprino/alfio-claude-plugins` | python-refactor (sql_generator 에서 복사) |

## 에이전트 — 12개

| 에이전트 | 역할 | 모델 | SubagentStart 주입 |
|---|---|---|---|
| `graph-architect` | 그래프·State·체크포인터·승인 흐름 구현 | opus | ✅ |
| `api-implementor` | FastAPI 레이어 구현 | sonnet | ✅ |
| `ingest-implementor` | ② 입력 처리 구현·추출 품질 개선 | opus | ❌ (settings.json matcher 미등록) |
| `privacy-auditor` | 개인정보 경로 점검 (읽기 전용) | sonnet | ✅ |
| `ontology-reference` | 온톨로지 조회 (읽기 전용) | haiku | ✅ |
| `code-reviewer` | CLAUDE.md 불변식 기반 리뷰 (읽기 전용) | sonnet | ✅ |
| `test-writer` | pytest (네거티브 필수) | sonnet | ✅ (+ 품질검사) |
| `git-committer` | Conventional Commit (push 제외) | sonnet | ✅ |
| `git-pusher` | push (확인 후) | sonnet | ✅ |
| `github-issue` | gh 이슈 | sonnet | ✅ |
| `github-pr` | gh PR | sonnet | ✅ |
| `python-refactor-agent` | 지표 기반 리팩토링 | inherit | ❌ |

예정: `claims-implementor`, `retrieve-implementor`, `spec-implementor`, `prompt-engineer`, `eval-runner`.

## 훅 — 11개 (전부 `.claude/settings.json` 등록)

| 이벤트 | matcher | 훅 | 동작 |
|---|---|---|---|
| SessionStart | — | session-context.sh | 브랜치·미커밋 변경 표시 |
| PreToolUse | Edit\|Write | check-python-rules.sh | snake_case·`__future__` 강제 |
| PreToolUse | Edit\|Write | protect-prompt-versions.sh | 기존 `prompts/*/vN.md` 수정 차단 |
| PreToolUse | Edit\|Write | check-pseudonymize-order.sh | ingest·posting_profile·pseudonymize 의 LLM 사용 차단, 프롬프트 하드코딩 경고 |
| PreToolUse | Bash | block-dangerous-commands.sh | `rm -rf /`, `rm -r data`, `git clean -x`, force push 차단 |
| PreToolUse | Bash | block-dotenv-access.sh | `.env` 열람·소싱, API 키 덤프 차단 |
| PreToolUse | Bash | block-applicant-data.sh | `git add data/`·`-f`·문서 원본 차단, data/ 미ignore 시 add 차단 |
| PostToolUse | Edit\|Write | auto-format-python.sh | ruff format + fix |
| Stop | — | remind-tests.sh | 테스트 리마인드 |
| SubagentStart | 10 유형 | subagent-context.sh | 에이전트별 규칙 주입 |
| SubagentStop | test-writer | subagent-quality-check.sh | 네거티브 테스트 누락 시 되돌림 |

권한: `settings.json` `permissions.deny` 가 `.env`·`.env.local`·`.env.*.local`·`.env.production`, DB 비밀번호(`data/secrets/**`),
가명화 매핑(`data/pseudonymized/**/mapping.json`) Read 를 막는다. 템플릿 `.env.example` 은 읽고 고칠 수 있다.
