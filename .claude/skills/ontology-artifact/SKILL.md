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
    --schema-sql data/ontology/src/scripts/db/schema_pg.sql [--target test]
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
| NCS 범위 | 온톨로지 뷰 `v_position_competency_ncs` 와 같은 규칙: 셀 `cell_ncs` → primary, 시범 직군 + 보조 직군 `sub_ncs` → secondary, 그 외 제외. (역량, 직군, 업종)으로 받아 새 공고에도 씀. 623 포지션 전부 뷰와 일치 확인 |
| NCS 본문 | State 에는 능력단위·요소·수행준거·KSA **건수**만, 본문은 명세 생성 때 `v_ncs_unit_detail` 조회 |
| ESCO | `competency_esco.representative` 만 (broad 제외) |

### 새 공고 (경로 2) [확정: 2026-11-02 — 방향, 구현은 다음 작업]

온톨로지에 없는 공고도 처리한다 (C안). P1 이 **임시(provisional) 프로필**을 만든다:
- 직군·업종: 공고 원문으로 **자동 추천 → 요청자 확인** (목록: 직군 59 · 업종 10)
- 절 분리(담당업무/자격요건/우대): 규칙 → 안 되면 LLM (공고는 PII 아님)
- 문장 → 역량·도구: 온톨로지 레포 매처 + 적재 산출물 규칙 (#59 §2 (b) 사본, 원본 커밋 기록). LLM 아님
- `source="provisional"` 표시, 명세 승인 화면에 '미검수 공고 분석'. 온톨로지 레포 검수 대상으로 환류
- 평가: 온톨로지 공고를 '새 공고처럼' 경로 2 로 돌려 검수 결과와 일치율 측정

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
