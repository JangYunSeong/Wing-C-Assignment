# 같은 공고 묶음 — 임시 공고를 실제 공고에 잇기, 실행 때 문제 고르기

## 왜

- 공고를 올리기 전에 채용담당자 인터뷰부터 하면, 그때 만든 **임시 공고**(`prov-…`)에 공통 과제가 승인된다. 나중에 실제 공고(온톨로지 공고)가
  올라오면 지원자 실행은 실제 공고 id 로 들어와 그 과제를 찾지 못한다 (Wing 요청 2026-11-20).
- **같은 공고여도 문제가 다를 수 있다.** 운영의 스마일게이트 "회원/게임 BE API 개발" 임시 공고 2건은 원문 해시까지 같은데 문제는
  서로 다르다 (목록 조회 N+1 · 커넥션 풀 교착, 둘 다 승인). 공고 하나에 승인된 인터뷰 과제는 하나라서(2026-11-19) 같은 공고로 다른
  문제를 만들려면 공고를 따로 등록해 왔다.

## 결정

- **같은 공고에 문제가 여럿이면 실행 만들 때 담당자가 고른다** [확정: 2026-11-20 사용자]. 고르지 않으면 지금처럼 공고의 인터뷰 과제 →
  재사용 → 생성. 같은 채용 회차 지원자에게 같은 문제를 주는 것은 고르는 담당자 몫이다 — 실행 화면에 고른 문제(`common_id`)가 보인다.
- **같은 회사·같은 원문 다시 등록은 알리고, 확인하면 따로 등록** [확정: 2026-11-20 사용자] — `POST /postings/suggest` 가
  `existing_posting_ids`·`similar_posting_ids` 로 미리 알리고, `POST /postings` 는 409 `posting_duplicate`, `allow_duplicate: true` 면 만든다.
- **잇기는 옮기지 않는다.** 승인된 공통 과제·회사 사례·빌드의 `posting_id` 를 고쳐 쓰지 않는다 — 승인 이력(누가 무엇을 어느 공고에
  승인했나)이 남고, `common_task` 의 유일 키·공고마다 인터뷰 과제 하나 인덱스와 부딪히지 않는다. 문제는 각자의 공고에 그대로 있고,
  묶음은 "실행 만들 때 고를 수 있는 문제의 범위"만 넓힌다.

## 구조

```sql
CREATE TABLE IF NOT EXISTS public.posting_link (
    posting_id text PRIMARY KEY,   -- 이어진 공고 (임시 공고, 같은 공고로 따로 만든 임시 공고)
    linked_to  text NOT NULL,      -- 기준 공고 (보통 실제 공고)
    linked_by  text NOT NULL,      -- TODO(auth) — 지금은 요청 본문
    linked_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (posting_id <> linked_to)
);
```

- **한 단계만** — 기준 공고는 다른 공고에 이어지지 않고, 다른 공고가 이어진 공고는 기준 공고에만 잇는다 (사슬 없음).
- 묶음 = 기준 공고 + 거기 이어진 공고 (`PostingStore.posting_group`). 묶음 어느 공고로 실행을 만들든 묶음의 승인된 문제에서 고른다.
- 회사가 둘 다 정해져 있으면 같은 회사여야 잇는다 (`postings/company.company_id`).
- 고를 수 있는 문제 = 묶음 공고들에 **실행 온톨로지 판**으로 승인된 공통 과제 (`CommonTaskRepo.approved_tasks`) — 다른 판의 과제는
  역량·수행준거 id 가 그 판 것이라 쓰지 않는다 (불변식 4).

## API

| 요청 | 하는 일 |
|---|---|
| `PUT /postings/{id}/link` `{linked_to, linked_by}` | 잇기 → `{posting_id, linked_to, group}`. 404 공고 없음 · 409 `posting_link_conflict`(사슬·회사 다름·이미 다른 공고에 이어짐·자기 자신) |
| `DELETE /postings/{id}/link` | 풀기 (없으면 그대로). 그 사이 만든 실행은 받은 문제 그대로 |
| `GET /postings/{id}/tasks` | 고를 수 있는 문제 — `common_id, posting_id(승인된 공고), title, task_kind, source_incident, interview, revision, approved_by, approved_at`, 최근 승인 순 |
| `POST /runs` 의 `common_id` | 담당자가 고른 문제. 묶음에 실행 판으로 승인된 과제가 아니면 422 `common_task_not_choosable` (지원서를 저장하기 전에) |

## 그래프

- `RunState.common_id` (예전 체크포인트는 None). draft_spec 은 고른 과제를 인터뷰 과제처럼 그대로 쓴다 — 만들지도 고치지도 않고,
  공통 과제를 다시 만드는 수정 요청은 막는다(API 409 · 노드 `chosen_common_locked`). 공고의 인터뷰 과제보다 먼저.
- 고르면 공고의 인터뷰 과제 판 검사(`interview_task_version_mismatch`)는 하지 않는다 — 고른 과제가 실행 판으로 승인된 것을 API 가 확인했다.
- 승인 전 확인(`replaced_common`)은 고른 과제를 기준으로 본다. finalize 는 고른 과제를 다시 승인하지 않는다 (원래 승인 기록 유지).
- 고른 과제가 그 사이 저장소에서 사라졌으면 다른 과제로 바꾸지 않고 공통 과제 없는 명세 + 오류 `chosen_common_gone`.

## 남은 것 [미정]

- 실제 공고가 올라오면 자동으로 이을지 — 같은 회사·같은 원문 해시면 후보로 알리기만 하고(권함) 잇는 것은 사람이.
- 묶음 단위의 인터뷰 과제 하나 규칙 — 지금은 공고마다 하나라, 묶음 안에 인터뷰 과제가 여럿일 수 있다 (고르면 되므로 막지 않음).
- 고르기를 필수로 할지 — 지금은 선택(고르지 않으면 공고 기본 과제). 묶음에 문제가 둘 이상이면 422 로 고르게 하는 것은 Wing 이
  고르기 화면을 붙인 뒤.

## 하지 않는 것

- 공통 과제·사례·빌드를 다른 공고로 옮기기 (승인 이력이 사라진다).
- 온톨로지 쪽에 임시 공고를 써 넣기 (불변식 4 — 온톨로지는 읽기만).
