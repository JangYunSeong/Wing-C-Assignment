# 손 구현 — 재료 batch-rerun (`pub:data.eng:batch-rerun-late-partial`)

손 구현 관문 ⑫(#256 3단계, `starter/hand_refs.py`)가 이 재료로 만드는 모든 빌드의 공개·숨은 테스트에 돌리는 사람이 쓴 구현이다.
LLM 이 만든 것이 아니고 지원자 데이터도 없다 — 공개 사례 재료의 손 예시 `daily-agg-pipeline`(개발 서버 `data/feature_starters/`, git 밖)에서 옮겼다.
손 예시의 샘플·숨은 데이터(약 45MB)와 숨은 채점은 옮기지 않았다.

## 구성

| 폴더 | 종류 | 놓친 사유 | 하는 일 |
|---|---|---|---|
| `accepted-swap` | 허용 (설계 1) | — | 받은 폴더 전부로 모든 날짜를 새로 집계, 파티션 폴더 교체 |
| `accepted-sql` | 허용 (설계 2) | — | 이번 폴더에 나온 결제 날짜들을 앞 폴더까지 모아 새로 집계 |
| `accepted` | 허용 (설계 3) | — | 최근 4일을 새로 집계 (손 예시 샘플의 최대 지연 3일 근거) |
| `naive` | 흔한 구현 | 셋 다 | paid_at 날짜가 실행 날짜인 레코드만 셈 |
| `naive-run-date` | 흔한 구현 | 셋 다 | 그날 폴더 전부를 실행 날짜 파티션에 셈 (2026-11-23 알파 kb002 r1 의 생성 흔한 구현 모양) |
| `naive-by-paid-date` | 흔한 구현 | 셋 다 | 그날 폴더를 paid_at 날짜별로 세어 그 파티션마다 새 파일 (재료 `naive_impl` 문장 · 알파 kb002 r2b 모양) |
| `naive-no-restate` | 하나만 놓침 | late_data_missed | 앞 날짜 파티션을 새로 집계하지 않음 |
| `naive-no-restate-by-date` | 하나만 놓침 | late_data_missed | 결제 날짜별로 다시 쓰는 모양 — 그날 폴더 결제만으로 날짜 파티션을 만들어 앞 폴더 결제를 합치지 않음 |
| `naive-no-dedupe` | 하나만 놓침 | duplicate_counted | 원시 줄 하나를 결제 한 건으로 셈 |
| `naive-nonatomic` | 하나만 놓침 | partial_output_visible | 파티션 파일을 지우고 제자리에 바로 씀 |

`_shared/pipeline/` 은 손 예시 시작 코드의 도우미(`raw.py` 원시 읽기 · `reader.py` 읽는 쪽 계약)다. 모든 손 구현 밑에 깔린다.
손 예시의 `hardcoded`(샘플 정답을 박은 구현)는 생성된 테스트로는 시험이 되지 않아 넣지 않았다. 실험 탐침 3개도 넣지 않았다.

## 확인 기록

- 2026-11-06 손 예시 REPORT §3 — 11개 대상 검증 관문 ①~⑨ 통과 (허용 셋 통과, 하나만 놓친 셋은 자기 열에서만, 흔한 구현은 t1·t2·t3).
- 2026-11-23 #256 1단계 알파 — 이름 변환 없이 생성 테스트에 대조: woori001 r3 은 이상적인 행렬, 11-18(인터페이스 고정 전) 테스트에는 모두 실패.
- 2026-11-24 새 흔한 구현 2개(`naive-run-date`·`naive-by-paid-date`)를 11-06 손 채점으로 확인 — 공개 기준 통과, t1·t2·t3 에서 의도한 태그로 실패.
- 2026-11-24 `naive-no-restate-by-date` — 오프라인 재생에서 `naive-by-paid-date` 가 kb002 r1 의 늦은 데이터 케이스 hc2 를 통과해(앞 날짜에 먼저 받은 결제가 없는 장면) 더했다. 11-06 손 채점: 공개 기준 통과, t1 실패, t2 통과. t3 는 늦은 레코드가 섞인 데이터셋에서만 같이 틀린다 (앞 날짜 파티션을 통째로 갈아서 — 사본만 있는 h2-dup 은 통과).
- 2026-11-24 #256 3단계 알파 4회(qwen, kb002·woori001) — 손 관문 수리 0, 빌드 4/4 review. 허용 셋은 모든 테스트 통과, 하나만 놓친 넷은 모두 자기 사유 케이스에서 실패. woori001 1회차는 사유를 섞은 케이스 표시(H③') 3건.
- 2026-11-24 #256 재료 옮기기 — 인터페이스의 `public_rules` 를 재료 숨은 안내(`hidden/data.eng.batch-rerun.yaml`)로 옮겨 지문만 바뀌었다 (명령·입출력 파일·엔티티는 그대로라 손 구현은 다시 확인하지 않음).

## 바꿀 때

- 인터페이스(`interfaces/data.eng.batch-rerun.yaml`)를 고치면 손 구현을 다시 확인하고 `references.yaml` 의 `interface_sha256` 을 고친다
  (`starter.interface.interface_ref`). 지문이 다르면 관문이 꺼지고 승인 화면에 표시된다. 단위 테스트도 이 지문을 확인한다.
- 손 구현을 더하면 `references.yaml` 에 종류·놓친 사유·설명을 적는다. 설명(note)은 숨은 테스트 수리 요청·승인 화면에 나간다 —
  재료의 trigger_terms·leak_terms·stage2_cause_terms·hidden_terms 를 쓰지 않는다 (단위 테스트가 확인).
- 확인은 손 예시의 손 채점(`run_all.sh`)과 오프라인 재생(`eval/starter/replay_hand_gate.py`)으로 한다.
