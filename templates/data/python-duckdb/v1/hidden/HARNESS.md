# 채점 하네스 API (data/python-duckdb v1)

P1 내부 문서다 (`gen_hidden` 입력에 들어감). 채점용 이미지에만 있고 응시용 이미지에는 없다.

숨은 테스트는 **숨은 요구가 지켜졌는지**를 배치 작업의 공개 계약(명령·옵션·종료 코드·입력 파일·출력 파일)으로 확인하는 pytest 테스트다.
케이스마다 임시 작업 폴더에 **작은 입력 파일을 직접 쓰고**, 작업을 별도 프로세스로 실행한 뒤, 출력 파일을 계약 형식으로 읽어 판정한다.
실행 순서(날짜 순서·다시 실행·쓰기 중 크래시)는 테스트가 정한다 — 실제 시간 지연이나 무작위에 기대지 않는다.

## 케이스 파일

- 위치 `grading/cases/test_<케이스 id>_<이름>.py`. **HiddenPlan 케이스 하나 = 파일 하나**, 테스트 함수 이름·모듈 설명(docstring) 첫 줄에 케이스 id(`hc1` …).
- import 는 하네스(`from harness.grader import …`), **이 과제의 계약 도우미(`from cases.contract import …`)**, pytest·표준 라이브러리만.
  **지원자 구현(`pipeline/…`)을 import 하지 않는다** — 공개 계약으로만 판정한다. 조절값 파일(`grading/grader-params.json`)은 쓰지 않는다.

## 계약 도우미 (`from cases.contract import …`)

빌드 파이프라인이 공개 계약에서 만든다 (이 문서 끝에 붙은 `contract.py` — 쓰지 않는다). 명령·옵션·데이터 모양을 손으로 쓰지 않고 이것을 쓴다.
- `run_<명령>(…옵션…, crash_after_bytes=None, timeout=None)` → `JobResult` — 계약의 명령을 계약의 옵션으로 실행 (옵션 이름은 계약 필드 이름 그대로).
- 데이터 모양 `TypedDict` (계약 entities) — 입력 레코드를 만들 때 필드 이름을 틀리지 않게.

## 하네스 (`from harness.grader import …`)

**작업 폴더 — `ws = workspace()`** (케이스마다 새 임시 폴더, 이름은 과제를 드러내지 않음)
- 입력 쓰기: `ws.write_jsonl(상대경로, 행들)` · `ws.write_csv(상대경로, 행들, columns=None)`(머리글은 열 이름으로 늘 씀) · `ws.write_parquet(상대경로, 행들, {"열": "string|int|float|bool|date|timestamp"})` · `ws.write_text` · `ws.write_json`
- 경로: `ws.path("raw")`(만들지 않음 — 출력 폴더처럼 작업이 만들 곳) · `ws.dir("raw")`(만듦) — `ws / "raw"`·`str(ws)`·`f"{ws}/raw"` 도 같은 작업 폴더 경로다. 명령에는 늘 이렇게 만든 경로를 넘긴다 (문자열 `"raw"` 그대로는 저장소 루트 기준이 된다)
- 출력 읽기: `ws.read_parquet(파일 또는 폴더)`(폴더면 하위 파티션 폴더 `date=…/` 까지 `*.parquet` 모두 — 읽는 쪽이 보는 것과 같게, 이름이 `_`·`.` 로 시작하는 파일·폴더는 뺌) · `ws.read_jsonl` · `ws.read_csv` · `ws.read_json`
  · `ws.partitions(상대경로, "date")`(`<경로>/date=<값>/` 의 값들) · `ws.snapshot(상대경로)`(파일마다 해시 — 실패 전후 비교) · `ws.files(폴더)`(하위 폴더까지 파일 `Path` 목록)
- 깨진 출력 파일을 읽으면 예외가 난다 — 크래시 뒤 화면을 볼 때는 `try/except` 로 "읽을 수 없음"을 값으로 바꾸고, **그 값이 보이면 실패**로 단언한다 (예외로 끝내지 않는다 — 아래 판정 규칙 "크래시 뒤").

**실행 결과 — `JobResult`**: `code`(종료 코드, 시간 초과면 None, 시그널로 죽으면 음수) · `ok`(code == 0) · `crashed` · `timed_out` · `stdout` · `stderr` · `last_error()`
- 쓰기 중 크래시: `run_…(…, crash_after_bytes=128)` — 그만큼 넘게 쓰는 순간 프로세스가 죽는다 (정리 코드도 돌지 않음 — 실제 장애처럼).

**실패 사유 — `reason(code, description)`**: `assert 값 == 기대, reason('late_data_missed', '늦게 온 9/1 레코드가 빠짐')`.
코드는 아래 숨은 테스트 입력의 "실패 사유 코드" 목록 안의 것만. **조절값 — `params.int(name, fallback)`·`params.str`·`params.list`**.

## 판정 규칙

- **기대 값은 계약의 정의에서만**: 입력 레코드로 정의대로 계산한 값을 테스트 안에서 만들어 비교한다 (작은 입력이라 손으로 셀 수 있게).
- **출력 전체를 비교한다**: 모든 날짜(파티션)를 읽어 정의대로 계산한 전체와 비교한다 — 한 날짜만 보면 다른 날짜에 잘못 들어간 값(사본·늦은 레코드를 실행 날짜에 넣은 것)을 놓친다.
- **판정 전에 작업이 동작했는지 먼저 확인한다** — 정상 입력으로 실행해 종료 코드 0 과 출력이 정의대로인 것을 (사유 없이) 단언한 뒤 숨은 요구를 시험한다.
  "출력이 없다"만 단언하면 아무것도 안 하는 시작 코드도 통과한다.
- **의도한 사유는 그 실패만 걸리는 단언에 붙인다.** 한 케이스에 다른 숨은 요구를 섞지 않는다 (예: 크래시 케이스에 늦은 데이터·사본을 넣지 않는다 —
  다른 요구를 놓친 구현이 이 사유로 떨어진다).
- **공정하게**: 허용 설계(전부 다시 계산·바뀐 날짜만 다시 계산·폴더 바꿔 끼우기 등)는 모두 통과해야 한다. 출력의 파일 이름·개수·쓰는 방식은 재지 않고,
  계약이 정한 읽는 방법으로 읽은 **값**만 비교한다.
- **정상 흐름은 받은 날짜 순서대로 하루씩 실행한다.** 날짜 폴더를 썼으면 그 날짜로 한 번 실행한다 — 여러 날짜 폴더를 써 두고 마지막 날짜로 한 번만
  실행해 앞 날짜 출력까지 기대하지 않는다 (허용 설계 중 '이번 폴더에 나온 날짜만 다시 계산'도 통과해야 한다. 정적 검사가 되돌려 보낸다).
  늦게 온 데이터는 앞 날짜를 실행한 뒤, 다음 날짜 폴더에 앞 날짜 레코드를 넣고 다음 날짜를 실행한다.
- **크래시 뒤**: 실패한 실행 뒤 읽는 쪽이 보는 출력은 **직전 성공 상태 그대로**여야 한다 — 직전에 없던 날짜는 없음, 있던 날짜는 직전 값.
  읽을 수 없는 파일·반쯤 쓴 값·빈 결과가 보이면 그것이 실패(partial_output_visible)다. 이어서 같은 날짜를 다시 실행하면 완전한 값.
- 테스트끼리 서로 독립 — 케이스마다 `workspace()` 를 새로 만든다.

## 예시 (모양만 — 과제마다 계약에 맞춘다)

```python
"""hc1 늦게 들어온 앞 날짜 결제가 그 날짜 요약에 반영된다"""

from harness.grader import reason, workspace
from cases.contract import Payment, run_pipeline_run


def summary(ws) -> dict:
    out = {}
    for day in ws.partitions("out/daily_sales", "date"):
        for r in ws.read_parquet(f"out/daily_sales/date={day}"):
            o, a = out.setdefault(day, {}).get(r["store_id"], (0, 0))
            out[day][r["store_id"]] = (o + r["orders"], a + r["sales_amount"])
    return out


def test_hc1_late_payment_restates_earlier_day() -> None:
    ws = workspace()
    p1 = Payment(payment_id="P1", store_id="S001", paid_at="2026-09-01 10:00:00", amount=1000)
    ws.write_jsonl("raw/2026-09-01/batch-0001.jsonl", [p1])
    assert run_pipeline_run(date="2026-09-01", raw=ws.path("raw"), out=ws.path("out")).ok
    assert summary(ws) == {"2026-09-01": {"S001": (1, 1000)}}  # 동작 확인 (사유 없이)
    late = Payment(payment_id="P2", store_id="S001", paid_at="2026-09-01 23:50:00", amount=700)
    ws.write_jsonl("raw/2026-09-02/batch-0001.jsonl", [late])
    assert run_pipeline_run(date="2026-09-02", raw=ws.path("raw"), out=ws.path("out")).ok
    assert summary(ws)["2026-09-01"]["S001"] == (2, 1700), reason("late_data_missed", "늦게 온 9/1 결제가 9/1 요약에 빠짐")
```
