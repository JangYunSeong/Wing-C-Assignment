# 공개 테스트 작성용 (data/python-duckdb v1)

P1 내부 문서다 (`gen_public` 입력에 들어감). 채점용 이미지에만 있고 응시용 이미지에는 없다.

공개 테스트는 **공개 수용 기준(p\*)이 지켜지는지**를 배치 작업의 공개 계약(명령·옵션·종료 코드·입력·출력 파일)으로 확인하는 기능 테스트다.
시작 코드·참조 구현이 이 테스트를 보고 맞춘다(테스트 먼저). 숨은 요구(늦게 온 데이터·사본·쓰기 중 실패·다시 실행 같은 것)는 시험하지 않는다.

## 케이스 파일

- 위치 `public_tests/test_<pt id>_<이름>.py`. **계약의 공개 테스트(pt\*) 하나 = 파일 하나**, 테스트 함수 이름·모듈 설명 첫 줄에 pt id(`pt1` …).
- 하네스(`from harness.grader import workspace, …`)와 **이 과제의 계약 도우미(`from public_tests.contract import …`)** 를 쓴다
  — 빌드 파이프라인이 공개 계약에서 만든다 (이 문서 끝에 붙은 `contract.py`, 쓰지 않는다). 명령은 `run_<명령>(…)`, 데이터 모양은 `TypedDict`.
- 지원자 구현(`pipeline/…`)을 import 하지 않는다. 입력은 `ws.write_jsonl`·`write_csv`·`write_parquet`, 출력은 `ws.read_parquet`·`read_jsonl`·`partitions`.

## 하네스 (`from harness.grader import workspace`) — 이 이름·인자만 쓴다

**작업 폴더 — `ws = workspace()`** (케이스마다 새 임시 폴더. `with` 로 쓰지 않는다)
- 입력 쓰기: `ws.write_jsonl(상대경로, 행들)` · `ws.write_csv(상대경로, 행들, columns=None)`(머리글은 열 이름으로 늘 씀) ·
  `ws.write_parquet(상대경로, 행들, {"열": "string|int|float|bool|date|timestamp"})` · `ws.write_text(상대경로, 글)` · `ws.write_json(상대경로, 값)`
- 경로: `ws.path("raw")`(만들지 않음 — 출력 폴더처럼 작업이 만들 곳) · `ws.dir("raw")`(만듦). `ws / "raw"`·`str(ws)` 도 같은 작업 폴더 경로다.
  **명령에는 늘 이렇게 만든 경로를 넘긴다** — 문자열 `"raw"` 그대로는 저장소 루트 기준이 되어 입력을 찾지 못한다 (명령은 저장소 루트에서 돈다).
- 출력 읽기: `ws.read_parquet(파일 또는 폴더)`(폴더면 하위 파티션 폴더 `date=…/` 까지 `*.parquet` 모두, 이름이 `_`·`.` 로 시작하는 파일·폴더는 뺌) · `ws.read_jsonl` · `ws.read_csv` · `ws.read_json`
  · `ws.partitions(상대경로, "date")`(`<경로>/date=<값>/` 의 값들 — 둘째 인자는 파티션 열 이름, 꼭 준다) · `ws.files(폴더)`(하위 폴더까지 파일 `Path` 목록)

**실행 결과 — `JobResult`**: `code`(종료 코드) · `ok`(code == 0) · `stdout` · `stderr` · `last_error()` — 단언 메시지에 `r.last_error()` 를 붙이면 실패 원인이 보인다.

## 판정 규칙

- 기대 값은 계약의 정의에서만 — 작은 입력으로 정의대로 계산한 값을 테스트 안에서 만들어 비교한다.
- 입력은 깨끗하게: 날짜 순서대로 한 번씩 실행, 레코드는 제 날짜 폴더에, 사본 없이. **크래시 주입(`crash_after_bytes`)·같은 날 다시 실행·늦게 온 레코드·사본은
  쓰지 않는다** — 숨은 요구 영역이다.
- 올바른 구현이 통과하게 — 확인 방법(pt 의 checks)이 말하는 결과를 그대로 단언한다. 출력 파일의 **이름·개수·쓰는 방식은 재지 않고** 읽은 값만 비교한다.
- 입력 파일 이름은 계약이 정하지 않았으면 특정 이름(`payments.csv` 등)에 기대지 않게 — 날짜 폴더마다 `batch-0001.csv` 처럼 쓰고, 한 폴더에 둘 이상 둬도 된다.
- 날짜 폴더를 썼으면 그 날짜로 한 번씩 실행한다 (마지막 날짜로 한 번만 실행해 앞 날짜 출력까지 기대하지 않는다). 테스트끼리 독립 (`workspace()` 를 케이스마다).

## 예시 (모양만 — 과제마다 계약에 맞춘다)

```python
"""pt1 날짜별·매장별 매출 합계와 건수가 정의대로"""

from harness.grader import workspace
from public_tests.contract import Payment, run_pipeline_run


def test_pt1_daily_store_summary() -> None:
    ws = workspace()
    ws.write_csv("raw/2026-09-01/batch.csv", [
        Payment(payment_id="P1", store_id="S001", paid_at="2026-09-01 10:00:00", amount=1000),
        Payment(payment_id="P2", store_id="S001", paid_at="2026-09-01 11:00:00", amount=500),
    ])
    r = run_pipeline_run(date="2026-09-01", raw=ws.path("raw"), out=ws.path("out"))  # 날짜마다 한 번씩
    assert r.ok, r.last_error()
    rows = ws.read_parquet("out/daily_sales/date=2026-09-01")  # 그 실행 날짜의 출력만
    assert [(x["store_id"], x["orders"], x["sales_amount"]) for x in rows] == [("S001", 2, 1500)]
```
