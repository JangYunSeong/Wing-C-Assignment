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
> 그 전까지는 로컬 빌드한 `ontology.sqlite` 를 `data/ontology/dev/` 에 두고 버전을 `dev+<commit>` 로 기록한다.

## 산출물 배치

```
data/ontology/<version>/
  manifest.json            커밋, 규칙 해시, 학습 데이터 버전, 행 수
  ontology.sqlite          scripts/db/build_db.py 산출 (수행준거·KSA 포함)
  competency_cards.jsonl   역량별 라벨·nature·제외 메모·패턴 어휘·도구 별칭·공고 예문 5~10개·ESCO 설명·NCS 단위 정의
  rules/competencies/ rules/tools/ seed/tech_seed.json   매처 규칙
```

## 로딩·검증

- 설정 `ONTOLOGY_VERSION` 과 디렉토리 이름, `manifest.json`, sqlite `ontology_version` 테이블 값이 모두 일치해야 기동.
- `manifest.json` 의 행 수·규칙 해시를 실제와 대조 (불일치 시 기동 실패).
- sqlite 는 `mode=ro` URI 로 연다: `sqlite3.connect(f"file:{p}?mode=ro", uri=True)`.
- 블로킹이므로 노드에서는 `asyncio.to_thread` 로.

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

정확한 컬럼은 사용하는 버전의 sqlite 에서 `PRAGMA table_info` 로 확인한다. 이 표를 믿고 컬럼명을 추측하지 않는다.

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
