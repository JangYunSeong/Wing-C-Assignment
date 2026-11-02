---
name: ontology-artifact
description: 온톨로지 레포(job-competency-ontology)가 내보내는 버전 고정 산출물(ontology.sqlite, 역량 카드 JSONL, 매처 규칙, manifest.json)을 읽기 전용으로 쓰는 방법. 버전 고정·검증, 공고 프로필 조회, 셀(직군×업종) 범위 필터, 역량 → NCS 수행준거·KSA·ESCO 그래프 조인, 주요 테이블·뷰를 다룬다. src/task_spec/ontology/ 를 만들거나 고칠 때, ①공고 프로필·③평가 근거 노드를 구현할 때, 역량·NCS 데이터를 조회할 때 사용.
---

# ontology-artifact — 온톨로지 읽기

## 계약 (#59 §2)

- 온톨로지 레포가 **버전이 붙은 산출물**을 내보낸다: git tag `onto-vYYYY.MM.DD` + release asset 또는 공유 경로.
- 이 레포는 그 산출물을 **버전 고정해서 읽기만** 한다. 온톨로지 레포 경로를 import 하지 않는다.
- 모든 명세에 `ontology_version` 을 적는다.

> 상태(2026-11-02): 온톨로지 레포에 아직 tag·release·`manifest.json`·역량 카드 export 가 없다 (#59 §5 미완).
> 그 전까지는 로컬 빌드 → PG 적재 (아래 "현재 적재").

## 저장: PostgreSQL (task_db) [확정: 2026-11-02]

온톨로지는 sqlite 파일을 직접 열지 않고 **task_db 의 버전별 스키마**에서 읽는다.

```
온톨로지 레포 build_db.py → ontology.sqlite
   └→ task_spec.ontology.load_pg → task_db 스키마 onto_<버전 슬러그> + public.ontology_registry
        (온톨로지 레포의 schema_pg.sql 을 그대로 사용 — 이 레포는 스키마를 고치지 않는다)
```

- 적재: 스테이징 스키마 → 외래키 순서 COPY(boolean·jsonb·text[] 변환) → 행 수 대조 → 이름 교체. 실패해도 기존 버전 유지.
- 기록: `public.ontology_registry(version, schema_name, source_sha256, row_counts, loaded_at)`.
- 읽기: `OntologyRepository.open(DATABASE_URL, ONTOLOGY_VERSION)` — 세션 `default_transaction_read_only=on` + 버전 스키마
  search_path. 쓰기는 DB 가 거부한다. 고정 SQL(LiteralString)만, 입력은 params.
- 앱 기동: 설정 버전이 레지스트리에 없으면 기동 실패. `/healthz` 의 `ontology_resolved` 가 실제 버전.
- `ONTOLOGY_VERSION=dev` 는 개발용 별칭 — 가장 최근 적재 버전. 명세 `ontology_version` 에는 실제 버전 문자열을 적는다.

### 현재 적재 (2026-11-02, 방법 a — 로컬 빌드)

| 항목 | 값 |
|---|---|
| 버전 | `v2026-10-30e@b69d00f+dirty` (온톨로지 레포 HEAD b69d00f, train v2026-10-30e) |
| 스키마 | `onto_v2026_10_30e_b69d00f_dirty` (task_db, task_db_test) |
| 규모 | 35 테이블, 866,161 행, 151 MB (NCS 수행준거 196,658 · KSA 574,279) |
| `+dirty` | git 제외 원천(ESCO·NCS 정보망 DB·train)을 복사해 빌드한 표시. 규칙·코드 변경 없음 |

재현:
```bash
gh repo clone JangYunSeong/job-competency-ontology data/ontology/src -- --depth 1   # data/ 는 git 밖
# git 제외 원천 복사 (현재 서버의 온톨로지 작업 사본에서, 읽기 전용):
#   data/esco/v1.2.1/esco_v1.2.1.sqlite, data/ncs/ncs_infonet.sqlite, data/train/<버전>/
(cd data/ontology/src && python3.11 -I scripts/db/build_db.py)
uv run python -m task_spec.ontology.load_pg --sqlite data/ontology/src/data/build/ontology.sqlite \
    --schema-sql data/ontology/src/scripts/db/schema_pg.sql --rules-root data/ontology/src [--target test]
```

정식 산출물(태그 `onto-vYYYY.MM.DD`, manifest, release)이 생기면 그 sqlite 를 같은 적재기로 올리고 `ONTOLOGY_VERSION` 을 고정한다.

## 주요 테이블 (schema.sql 기준, 2026-11-02 확인)

| 묶음 | 테이블 |
|---|---|
| 메타 | `ontology_version(version, git_commit, train_version, built_at, sources)` |
| 직무 | `job_family`, `sub_family(in_pilot)`, `industry`, `company`, `job_node`, `position`, `cell(sub, industry_id)` |
| 역량 | `competency(id,label,area,status,nature)`, `competency_scope`, `competency_builds_on`, `competency_near`, `requires(job_node→competency)`, `competency_domain`, `competency_gap` |
| 도구 | `tool_family`, `tool`, `tool_role`, `enables(tool_family→competency)` |
| 공고 | `posting_line(position_id, section, text)`, `line_competency`, `line_tool` |
| NCS | `ncs_subd`, `ncs_unit`, `ncs_element`, `ncs_criterion`(수행준거), `ncs_ksa`, `cell_ncs`, `sub_ncs`, `competency_ncs` |
| ESCO | `esco_concept`, `competency_esco`, `job_node_esco`, `position_esco` |
| 뷰 | `v_competency_links`, `v_position_competency_ncs`, `v_ncs_unit_detail` |

정확한 컬럼은 적재된 스키마의 `information_schema.columns` 로 확인한다. 이 표를 믿고 컬럼명을 추측하지 않는다.

## ① 공고 프로필·③ 평가 근거 (`ontology/queries.py`) [확정: 2026-11-02]

| 항목 | 규칙 |
|---|---|
| 공고 식별 | `posting_id` = 온톨로지 `position.id` (경로 1). 없으면 API 404 `posting_not_found`, 그래프는 END |
| 셀 | `직군(sub)|업종(industry_id)` |
| 공고 역량 | `posting_line` ⋈ `line_competency` — 역량별 절·수준·근거 문장 id |
| 주/보조 | **담당업무·자격요건 = 주, 우대에만 = 보조** (사용자 결정) |
| 공고 도구 | `line_tool` ⋈ `tool` — `kind=language` 가 '공고 언어' (과제 기술 환경 입력) |
| NCS 범위 | 온톨로지 뷰 `v_position_competency_ncs` 와 같은 규칙: 셀 `cell_ncs` → primary, 시범 직군 + 보조 직군 `sub_ncs` → secondary, 그 외 제외. 셀에 목록이 없으면 직군 `sub_ncs` → sub_fallback (아래). (역량, 직군, 업종)으로 받아 새 공고에도 씀. 623 포지션 전부 뷰와 일치 확인 |
| NCS 본문 | State 에는 능력단위·요소·수행준거·KSA **건수**만, 본문은 명세 생성 때 `v_ncs_unit_detail` 조회 |
| ESCO | `competency_esco.representative` 만 (broad 제외) |

### 새 공고 (경로 2) [확정: 2026-11-02 — 구현: `src/task_spec/postings/`]

온톨로지에 없는 공고도 처리한다 (C안). 흐름:

```
POST /postings/suggest  원문(텍스트·PDF·DOCX·HWPX) + 회사·직무명 → 절 분리 + 직군·업종 후보 (posting_suggestion)
POST /postings          suggestion_id + 확인한 직군·업종 + confirmed_by → 매처 분석 → 임시 공고 prov-… (provisional_posting)
POST /runs              posting_id=prov-…  → posting_profile(source=provisional) → 이하 경로 1 과 같음
```

| 단계 | 방식 |
|---|---|
| 절 분리 (`sections.py`) | 절 제목 사전(업무내용·지원자격·우대사항 등, 묶음 제목·글머리 제거) → 담당업무·자격요건을 못 찾으면 LLM(`prompts/segment_posting`). LLM 문장은 원문에 글자 그대로 있어야 남김 |
| 업종 (`suggest.py`) | 등록 회사면 그 업종, 아니면 LLM(`prompts/suggest_field`) |
| 직군 | 0.5·직무명 유사도(포지션 623, 평가 땐 자기 제외) + 0.5·직군별 역량 문장 비율. 약하거나 접전이면 LLM 섞음. 상위 3 + 근거 |
| 확인 | 요청자가 직군·업종을 고른다 (`confirmed_by` — 인증 전까지 본문, TODO(auth)). 목록 밖 값 422 |
| 문장 분석 | 매처 사본 `postings/matcher.py` (원본 b69d00f, 로직 동일) + **PG `public.ontology_rules`(같은 버전, json)** |
| 임시 공고 | `public.provisional_posting` — 버전·규칙 해시·문장별 결과·미매칭 수, 상태 pending_review. 실행 버전과 분석 버전이 다르면 그래프 END(`posting_version_mismatch`) |
| 환류 | DB 저장까지. 온톨로지 레포로 내보내기는 보류 [미정] |
| LLM | 공고는 PII 아님 → openai:gpt-4.1-mini (속도). 규칙이 실패할 때만 |

검증한 것
- 매처 사본 = 온톨로지 결과: 시범 직군 공고 문장 2,765개 종류·역량·수준 100% (`tests/postings/test_postings_pg.py`)
- **규칙은 jsonb 가 아니라 json 으로 저장** — jsonb 는 키 순서를 바꾸고, 수준 규칙은 순서가 우선순위다 (jsonb 로 넣었을 때 40문장 수준이 틀렸다)
- 적재 후 **키 컬럼만 ANALYZE** — 통계 없으면 평가 근거 질의 4초+, 전체 ANALYZE 는 긴 한국어 텍스트 통계 계산에 10분+ (키 컬럼만: 테이블당 <1초, 질의 0.06초)

온톨로지에 NCS 목록이 없는 직군×업종 셀 [확정: 2026-11-02 — 사용자 결정 (a) + 경고]
셀 범위 NCS(`cell_ncs`)는 공고가 있던 48개 셀에만 있다 (시범 직군 조합 140개 중). 셀에 목록이 하나도 없으면
**직군 단위 `sub_ncs` 로 대체** — `NcsUnitRef.scope = "sub_fallback"` (셀보다 넓어 덜 정밀), 경고 `posting_cell_unseen`.
경로 1·2 모두 적용. 온톨로지 뷰에는 없는 이 레포 규칙이라 뷰 대조 테스트는 primary/secondary 끼리만 비교.

## 쓰임새

| 단계 | 질의 |
|---|---|
| ① 공고 프로필 | `position` → `posting_line` + `line_competency` / `line_tool` → 공고 역량 목록과 근거 문장 (온톨로지 레포의 공고 프로필 출력과 같은 결과여야 한다) |
| ③ T1 범위 필터 | 공고의 `sub_family` → `competency_scope` 로 후보 역량 범위 |
| ③ T1 어휘 후보 | 역량 카드의 패턴 어휘·도구 별칭 (매처 규칙) |
| ③ T1 벡터 후보 | 역량 카드의 **공고 예문**(라벨 아님 — 라벨 임베딩 정밀도 52%, ONTOLOGY.md §5.6). 임베딩은 이 서버에서 계산하지 않는다 |
| ③ 평가 근거 | 역량 → `competency_ncs` ∩ `cell_ncs`(셀 범위) → `ncs_element` → `ncs_criterion`·`ncs_ksa`; 역량 → `competency_esco` |

- NCS 는 **셀(직군×업종) 범위 안에서만** 고른다. 셀 밖 능력단위를 근거로 쓰지 않는다.
- ESCO 는 대표가 있을 때만 (broad·공백이면 생략, `context.md` §6).
- uncovered(온톨로지에 없는 역량)는 만들어내지 않고 그대로 uncovered 로 보고 → 온톨로지 레포에 역량 신설 후보 이슈.

## 매처 공유

매처(`competency_match.py`)는 [미정]: (a) 작은 패키지로 분리해 공유(권장) / (b) 사본 + 산출물 규칙만 교체.
결정 전까지 (b): `src/task_spec/ontology/matcher.py` 사본, 상단에 원본 커밋 해시를 적고 규칙은 산출물에서만 읽는다.
