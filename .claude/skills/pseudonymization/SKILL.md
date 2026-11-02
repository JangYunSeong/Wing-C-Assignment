---
name: pseudonymization
description: 이력서·자기소개서 가명화 규칙. 치환 대상(이름·연락처·학교·학력·링크·회사·제품·교육 과정·나이·성별 등), 일관 치환 토큰, 매핑 저장, 누락 검사, 가명화 타입(PseudonymizedText)으로 LLM·임베딩 이전 단계 강제, 로그·저장 규칙을 다룬다. src/task_spec/nodes/pseudonymize 나 관련 스키마를 만들거나 고칠 때, 지원자 데이터를 다루는 코드를 쓸 때, LLM 입력 경로를 바꿀 때 사용.
---

# pseudonymization — 가명화

## 구현 현황 [확정: 2026-11-02]

| 항목 | 결정 |
|---|---|
| 위치 | `src/task_spec/nodes/pseudonymize/` (패키지 — 훅이 LLM import 를 막는 경로) |
| 운영 가명화기 | `RulePseudonymizer(data_dir)` — 앱 lifespan 에서 생성·`warm_up()`. 그래프 기본값 stub 은 텍스트를 내보내지 않는다 |
| 입력 | ingest 추출본(`data/extracted/<run_id>/<doc_id>.txt`, `ExtractedRef`)만 — sha256 대조. 추출 실패는 같은 사유로 실패 |
| 사람 이름 | 호칭·직함 규칙 + Kiwi(kiwipiepy, 로컬) NNP 중 '성씨+이름' 3~4음절. 기술어·지명 허용목록으로 오탐 차단 |
| 사전 | `src/task_spec/resources/dicts/` 공개 목록 (대학·회사·교육과정·성씨·기술어). `!` 항목은 재직 맥락에서만 |
| 주소 | 라벨(`주소:`) 또는 행정구역 2단위 이상 + 도로명/동 + 번지 |
| 기간·날짜 | 그대로 둔다 (T2 기간 대조). 생년월일만 삭제 |
| 매핑 암호화 | [미정 → 운영 배포 전] 지금은 권한 600 + `settings.json` Read 거부 |
| 평가 | `eval/pseudonymize/` (eval-harness 스킬) — dev 98.4% / 숨김 test 97.5% (2026-11-02). 회귀: `tests/eval/`, `tests/pseudonymize/` |
| 보안 점검 | privacy-auditor 2회 (2026-11-02). C1(원문 토큰 모양으로 탐지 우회) 수정 — 탐지 전 `⟨…⟩` 로 무력화 + 미발급 토큰을 누락으로 집계. 회귀: `test_adversarial.py` |

알려진 한계: 사전·접미어 없는 회사(hard 0%), 라벨·따옴표 없는 서비스명(약 50~60%), 영문 이름, 성씨 목록 밖 이름,
한 단위짜리 라벨 없는 주소. 후속: `load_pseudonymized` 가 `data_dir` 하위인지까지 확인 (감사 W-a, 낮음).

## 원칙

- **임베딩·LLM 호출 전에** 가명화한다 (#59 §4, `context.md` §4.6·§7). 예외 없음.
- 민감 정보(나이, 학교, 성별 등)는 생성 파이프라인 진입 전에 제거 — 과제 난이도·내용에 영향을 줄 수 없게.
- 가명화 실패(누락 검사 불통과) 시 **실행을 멈춘다.** 부분 통과로 다음 단계에 넘기지 않는다.

## 치환 대상

| 범주 | 예 | 토큰 |
|---|---|---|
| 이름 | 지원자·동료·지도교수 | `[PERSON_1]` |
| 연락처 | 전화, 이메일, 주소 | `[CONTACT_1]` |
| 링크 | GitHub, 블로그, 포트폴리오 URL | `[LINK_1]` |
| 학교·학력 | 대학·고교명, 학과, 학위, 학점 | `[SCHOOL_1]` / 학위는 제거 |
| 회사 | 재직·인턴 회사, 고객사 | `[ORG_1]` |
| 제품·서비스 | 회사 내부 제품명, 서비스명 | `[PRODUCT_1]` |
| 교육 과정 | 부트캠프·사내 교육명 | `[COURSE_1]` |
| 인구통계 | 나이·생년, 성별, 병역, 사진, 출신지 | 제거 (토큰 없음) |

**치환하지 않는 것**: 기술·도구명(Python, Spark …), 직무 용어, 수치·기간(주장 추출·T2 대조에 필요).
기간·날짜는 그대로 둔다 [확정: 2026-11-02] — T2 가 기간 일치를 쓴다. 생년월일만 삭제.

## 일관성

- 한 실행(run) 안에서 같은 실체 → 같은 토큰. 이력서·자기소개서가 같은 `[ORG_1]` 을 써야 T2 연결이 된다.
- 회사·프로젝트명 표기 변형(약칭·영문)은 정규화 사전으로 묶는다.
- 토큰 번호는 등장 순서. 실행 간에는 공유하지 않는다.

## 탐지

1. 문서 구조 기반: 인적사항 칸, 학력 칸, 경력 머리말(회사·기간) — `ingest` 가 분리한 칸 정보를 쓴다.
2. 패턴: 이메일·전화·URL·주민번호 형태 정규식.
3. 사전: 대학 목록, 회사 목록(온톨로지 `company` 포함), 성씨+이름 패턴.
4. 로컬 형태소 분석기 Kiwi [확정] — **OpenAI·OpenCode 를 포함한 외부 API 사용 금지** (가명화 전 텍스트이므로).
   외부 LLM 은 가명화를 통과한 뒤에만 쓴다 (CLAUDE.md 불변식 2).

## 누락 검사

- 치환 후 텍스트에 1~3 탐지기를 **다시** 돌려 0건이어야 통과.
- 매핑의 원 값이 치환본에 부분 문자열로 남아 있지 않은지 검사.
- 결과 `PseudoCheck{passed, residual_count, detectors}` 를 State 에 남긴다 (잔여 값 자체는 남기지 않는다).

## 타입으로 강제

```python
class PseudonymizedText(str):
    """pseudonymize 노드만 생성할 수 있다. 생성자는 모듈 private."""
```

- 구현: `schemas/pseudo_text.py` — 생성에 모듈 private 토큰 `_MINT` 필요. 만드는 곳은 `nodes/pseudonymize.py` 의
  `mint_pseudonymized()`·`load_pseudonymized()`(검사 통과본만) 뿐. 입력 스키마 필드는 `PseudoTextField` 로 선언한다.
- `prompting.assemble()` 과 임베딩 클라이언트는 지원자 텍스트 인자를 `PseudonymizedText` 로만 받는다.
- 일반 `str` 을 넘기면 타입 검사(mypy/pyright)와 런타임 검사 둘 다에서 막힌다.

## 저장

- 가명화본: `data/pseudonymized/<run_id>/`.
- 매핑(토큰 → 원 값): `data/pseudonymized/<run_id>/mapping.json` — **State·로그·API 응답·LLM 입력에 넣지 않는다.**
  암호화 저장 여부·보존 기간 [미정].
- 명세·승인 화면은 토큰 그대로 보여 준다. 역치환이 필요한 화면은 [미정].

## 테스트

- 가상 이력서 픽스처(실존 인물·학교·회사 아님 표시)로: 범주별 치환, 문서 간 일관 토큰, 누락 검사 실패 → 실행 중단.
- 네거티브: 잔여 이메일·URL 이 남은 경우 검사 실패, `str` 을 assemble 에 넘기면 예외.
