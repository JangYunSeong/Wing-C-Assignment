---
name: document-ingest
description: ② 입력 처리 규칙 — 지원서 원본(PDF·스캔 PDF·DOCX·HWPX·TXT)에서 텍스트를 뽑고 레이아웃(2단·오른쪽 정렬 기간)을 복원해 절·머리말·칸(문제 정의/과정/기여한 점 등)으로 구조화한다. ⟦칸⟧ 표시 본문, ExtractedRef/ExtractedDocument, OCR(tesseract) 설정, 형식별 한계, 추출 평가를 다룬다. src/task_spec/nodes/ingest/ 를 만들거나 고칠 때, 새 형식·레이아웃을 지원할 때, 칸 라벨 표기를 늘릴 때, 추출 품질을 측정할 때 사용. PDF 일반 조작은 pdf 스킬을 본다.
---

# document-ingest — 텍스트 추출과 칸 분리

## 흐름 [확정: 2026-11-02]

```
업로드 data/raw/<run_id>/  →  ingest(FileIngestor)  →  data/extracted/<run_id>/<doc_id>.{txt,json} (600)
                           →  pseudonymize(추출본 .txt, sha256 대조)  →  extract_claims
```

- 가명화 이전 단계: LLM·임베딩·외부 API import 금지 (불변식 2, 훅이 막는다). 추출본엔 원문 PII 가 있다.
- State 에는 `ExtractedRef`(경로·해시·방법·절/칸 수·실패 사유)만. 전체 구조(`ExtractedDocument`)는 `.json` 파일.
- 실패(`unsupported_format` / `empty_text` / `extract_error` / `ocr_error`) → 가명화가 같은 사유로 실패 → END.
- 경로 가두기: run_id·doc_id 형식, 원본은 `data/raw/<run_id>/` 아래, 업로드 때의 sha256 과 일치해야 읽는다.

## 보안 (privacy-auditor 2026-11-02 반영)

- 노드 전 구간 예외 → 타입명만 로그, `extract_error` (pydantic 오류 메시지는 원문을 담는다).
- OCR: pytesseract 대신 `tesseract … stdout` 직접 호출. 입력 이미지는 `data/tmp/<run_id>/`(700, 600, O_EXCL) — 끝나면 폴더째 삭제, /tmp 에 남기지 않음. 타임아웃 60초.
- 상한: PDF 30쪽, 페이지 2000pt, ZIP(HWPX·DOCX) 압축 해제 50MB·항목 2000개, HWPX 섹션 10MB. XML 은 `defusedxml`.
- 업로드 원본도 700/600. `ExtractedRef.warnings` 는 코드 형식만.
- 보존: 원본·추출본(.txt/.json)·가명화본의 보존 기간·삭제 시점 [미정] — 운영 전 결정.

## 형식

| 형식 | 도구 | 비고 |
|---|---|---|
| PDF 텍스트층 | **pypdfium2** (PDFium, Apache/BSD) 글자 상자(loose) → 단어 → 줄 | pdfplumber/pdfminer 는 한글 PDF 에 흔한 CFF CJK 폰트에서 글자 0개 — 쓰지 않는다 |
| PDF 스캔본 | 페이지 글자 < 10 이면 tesseract 4.1 `kor+eng`, **400dpi, `--psm 6`** (직접 호출) | psm 3 은 여백 넓은 이력서에서 블록을 잘못 나눠 깨진다. 300dpi 는 한영 혼용 줄의 짧은 한글 라벨을 영문으로 오인 |
| DOCX | python-docx — 문단·표를 문서 순서대로, 표 행은 `셀 | 셀` | |
| HWPX | ZIP `Contents/section*.xml` 의 문단 직속 run/t (표준 라이브러리, 압축 해제 50MB 상한) | |
| TXT | UTF-8 | |
| HWP(구형 바이너리) | 미지원 → `unsupported_format` | 후보: LibreOffice+H2Orestart 변환. pyhwp 는 AGPL |

## 레이아웃 (`layout.py`)

- 2단: 페이지 30~70% 구간의 세로 빈 띠(≥12pt, 지나가는 단어 ≤3%) + 양쪽 글자 ≥25% → 왼쪽 단 → 오른쪽 단.
  좁은 날짜 열은 단으로 보지 않는다 (같은 줄 내용과 붙어야 머리말 기간이 된다).
- 줄: top 이 가까운 단어 묶음. 단어 간격 > 글자 크기×2 면 공백 두 칸 (오른쪽 정렬 기간).

## 구조 (`structure.py`)

- 절 제목 사전(경력·학력·프로젝트·교육·자기소개·기술·기타), 기간 있는 줄 = 머리말.
- 칸 라벨 정규형: `문제 정의`, `과정`, `기여한 점`, `담당업무`, `성과`, `사용 기술` (별칭은 `_LABELS`).
- 칸으로 보는 표기: `[라벨]`, `라벨:`/`-`/`|`, 라벨만 있는 줄, **상자형 글머리(■▶◆) 뒤 공백**.
  `-`·`•` 뒤 공백은 일반 문장("• 성과 지표를…")과 헷갈려 칸으로 보지 않는다. "과정에서…" 도 칸 아님.
- 본문 표시 `⟦라벨⟧ 내용` — 가명화 토큰 `[ORG_1]` 과 겹치지 않는 괄호. 인용 대조(llm/quotes.py)가 지운다.
- 역할(참여/주도/설계) 판정은 이 칸 구조를 입력으로 `assign_roles` 가 한다 (불변식 6, 다음 작업).

## 평가 (`eval/ingest/`, eval-harness)

시드 고정 생성기(레이아웃 7종 × 라벨 표기 변형) → 본문 일치·칸 재현율·칸 순서·잘못 만든 칸·기간·2단 순서.
2026-11-02: 텍스트층 6종 dev/test 모두 1.000. 스캔본 칸 재현율 dev 0.858 / test 0.660. 회귀: `tests/eval/test_ingest_eval.py`.
주의: dev·test 가 같은 생성기라 텍스트층 1.000 은 낙관적 — 실제 서식 다양성은 미측정.

## 알려진 한계

- 스캔본: 한영 혼용 줄 오인식(Spark→502), 2단 스캔본은 단이 섞일 수 있음(psm 6). 개선 후보: PaddleOCR.
- 구형 HWP 미지원, 표 안 셀 병합의 복잡한 서식, 머리글/바닥글 반복, 다중 페이지 2단 경계.
