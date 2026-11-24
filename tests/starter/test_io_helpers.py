"""계약 도우미의 입력 쓰기·출력 읽기 함수 (#256 2단계) — 재료 인터페이스가 정한 파일(Fixture.role·format·entity)에서 contract.py 에
write_…()·read_…() 를 만든다 (starter/py_helpers.py). 정적 검사는 도우미 호출의 날짜 인자를 날짜 흐름으로 읽는다 (starter/static_check.py)."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from task_spec.nodes.starter.plan_contract import fit_output
from task_spec.schemas import GeneratedFile
from task_spec.schemas.build import PlanContractOutput, TaskContract
from task_spec.starter.hidden_helpers import render_contract_case
from task_spec.starter.py_helpers import render_py_contract
from task_spec.starter.static_check import (
    future_input_days,
    unknown_py_keywords,
    unrun_input_days,
)
from task_spec.starter.templates import load_template
from tests.starter.test_data_template import _contract as _plain_contract
from tests.starter.test_material_interface import (
    _batch_seed,
    _clean_llm_contract,
    _iface,
    _manifest,
    _plan,
    _spec,
)

DE = load_template("data/python-duckdb")


def _fitted() -> TaskContract:
    out, _ = fit_output(
        PlanContractOutput(contract=_clean_llm_contract(), hidden_plan=_plan()),
        _manifest(),
        _spec(),
        _batch_seed(),
        interface=_iface(),
    )
    return out.contract


def _helper(contract: TaskContract, audience: str = "hidden") -> str:
    g = render_py_contract(contract, DE.manifest, audience)  # type: ignore[arg-type]
    assert g is not None
    compile(g.content, "contract.py", "exec")  # 문법만이 아니라 인자 중복 같은 컴파일 오류까지
    return g.content


def test_the_material_interface_fills_file_roles_that_the_llm_never_sees() -> None:
    c = _fitted()
    assert [(f.fixture_id, f.role, f.format, f.entity) for f in c.fixtures] == [
        ("f1", "input", "jsonl", "PaymentEvent"),
        ("f2", "output", "parquet", "DailySales"),
    ]
    props = PlanContractOutput.model_json_schema()["$defs"]["Fixture"]["properties"]
    assert not {"role", "format", "entity"} & set(props)  # plan_contract 출력 스키마는 그대로


def test_contract_py_gets_write_and_read_helpers_from_the_interface_files() -> None:
    src = _helper(_fitted())
    assert "'write_payment_events'," in src and "'read_daily_sales'," in src
    assert (
        'def write_payment_events(ws: Workspace, rows: Iterable[PaymentEvent], *, date: str, raw: str = "raw", '
        'name: str = "batch-0001.jsonl") -> Path:' in src
    )
    assert 'return ws.write_jsonl(f"{raw}/{date}/{name}", list(rows))' in src
    assert (
        'def read_daily_sales(ws: Workspace, *, out: str = "out") -> dict[str, list[DailySales]]:'
        in src
    )
    assert 'base = f"{out}/daily_sales"' in src and 'ws.partitions(base, "date")' in src
    assert "from harness.grader import JobResult, Workspace, run" in src
    # 공개 테스트 도우미에도 같은 함수 (인터페이스는 공개 계약이다)
    assert "def read_daily_sales(" in _helper(_fitted(), "public")


def test_without_file_roles_contract_py_is_what_it_was() -> None:
    """인터페이스 없는 재료(LLM 이 정한 계약) — 입출력 도우미를 만들지 않고 예전 contract.py 그대로."""
    src = _helper(_plain_contract())
    assert "def write_" not in src and "def read_" not in src
    assert "from harness.grader import JobResult, run" in src and "Workspace" not in src


def _with(contract: TaskContract, **change: Any) -> TaskContract:
    fixtures = [f.model_copy(update=change.get(f.fixture_id, {})) for f in contract.fixtures]
    return contract.model_copy(update={"fixtures": fixtures})


def test_csv_and_parquet_inputs_and_unpartitioned_outputs() -> None:
    c = _with(
        _fitted(),
        f1={"format": "csv", "path": "{raw}/{date}/*.csv"},
        f2={"format": "jsonl", "path": "{out}/summary/*.jsonl"},
    )
    src = _helper(c)
    assert "ws.write_csv(f\"{raw}/{date}/{name}\", list(rows), columns=['payment_id'," in src
    assert 'name: str = "batch-0001.csv"' in src
    assert 'def read_summary(ws: Workspace, *, out: str = "out") -> list[DailySales]:' in src
    assert 'return ws.read_jsonl(f"{out}/summary")' in src
    c = _with(_fitted(), f1={"format": "parquet", "path": "{raw}/{date}/*.parquet"})
    src = _helper(c)
    assert "ws.write_parquet(f\"{raw}/{date}/{name}\", list(rows), {'payment_id': 'string'," in src
    assert "'paid_at': 'timestamp'" in src and "'amount': 'int'" in src


class _Workspace:
    """데이터 템플릿 하네스 Workspace 와 같은 뜻의 작은 사본 (jsonl 쓰기·읽기·있음·파티션) — 진짜 하네스는 pyarrow 를 import 해
    개발 환경에서 못 불러온다. 진짜 하네스로는 손 예시 venv 에서 같은 동작을 확인했다 (2026-11-24, parquet 포함)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write_jsonl(self, rel: str, rows: list[dict[str, Any]]) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
        return p

    def read_jsonl(self, rel: str) -> list[dict[str, Any]]:
        p = self.root / rel
        if not p.exists():
            raise FileNotFoundError(rel)
        files = (
            [p]
            if p.is_file()
            else sorted(
                f
                for f in p.rglob("*.jsonl")
                if not any(x.startswith(("_", ".")) for x in f.relative_to(p).parts)
            )
        )
        return [
            json.loads(line) for f in files for line in f.read_text("utf-8").splitlines() if line
        ]

    def exists(self, rel: str) -> bool:
        return (self.root / rel).exists()

    def partitions(self, rel: str, key: str) -> list[str]:
        base = self.root / rel
        return sorted(
            d.name.split("=", 1)[1]
            for d in base.iterdir()
            if d.is_dir() and d.name.startswith(f"{key}=")
        )


def _load(src: str, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    grader = types.ModuleType("harness.grader")
    grader.Workspace, grader.JobResult, grader.run = _Workspace, object, None  # type: ignore[attr-defined]
    pkg = types.ModuleType("harness")
    pkg.grader = grader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "harness", pkg)
    monkeypatch.setitem(sys.modules, "harness.grader", grader)
    mod = types.ModuleType("contract_under_test")
    exec(compile(src, "contract.py", "exec"), mod.__dict__)
    return mod


def test_helpers_write_and_read_where_the_interface_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """쓰기 위치·이름 바꾸기·모든 파티션 읽기(이름이 _ 로 시작하는 폴더는 뺌)·없는 출력은 빈 dict."""
    c = _with(
        _fitted(), f2={"format": "jsonl", "path": "{out}/daily_sales/date=YYYY-MM-DD/*.jsonl"}
    )
    mod = _load(_helper(c), monkeypatch)
    ws = _Workspace(tmp_path)
    assert mod.read_daily_sales(ws) == {}
    row = {"payment_id": "P1", "store_id": "S001", "paid_at": "2024-06-01 10:00:00", "amount": 1000}
    p = mod.write_payment_events(ws, [row], date="2024-06-01")
    assert p == tmp_path / "raw" / "2024-06-01" / "batch-0001.jsonl"
    p2 = mod.write_payment_events(ws, [row], date="2024-06-01", name="batch-0002.jsonl")
    assert p2.name == "batch-0002.jsonl" and len(ws.read_jsonl("raw/2024-06-01")) == 2
    for day, n in (("2024-06-01", 2), ("2024-06-02", 1)):
        ws.write_jsonl(
            f"out/daily_sales/date={day}/part-0.jsonl", [{"store_id": "S001", "orders": n}]
        )
    ws.write_jsonl("out/daily_sales/_staging/x.jsonl", [{"store_id": "S999", "orders": 9}])
    assert mod.read_daily_sales(ws) == {
        "2024-06-01": [{"store_id": "S001", "orders": 2}],
        "2024-06-02": [{"store_id": "S001", "orders": 1}],
    }


HC_HELPERS = (
    "from harness.grader import reason, workspace\n"
    "from cases.contract import read_daily_sales, run_pipeline_run, write_payment_events\n\n\n"
    "def test_hc1_late() -> None:\n"
    "    ws = workspace()\n"
    "    write_payment_events(ws, [], date='2024-06-01')\n"
    "    assert run_pipeline_run(date='2024-06-01', raw=ws.path('raw'), out=ws.path('out')).ok\n"
    "    write_payment_events(ws, [], date='2024-06-02')\n"
    "    assert run_pipeline_run(date='2024-06-02', raw=ws.path('raw'), out=ws.path('out')).ok\n"
    "    assert read_daily_sales(ws) == {}\n"
)


def _case(src: str) -> list[GeneratedFile]:
    return [GeneratedFile(path="grading/cases/test_hc1_late.py", content=src)]


def test_day_flow_checks_read_the_date_given_to_the_write_helper() -> None:
    c = _fitted()
    assert unrun_input_days(_case(HC_HELPERS), c, "숨은 테스트") == []
    assert future_input_days(_case(HC_HELPERS), c, "숨은 테스트") == []
    # 앞 날짜 실행 전에 뒤 날짜 폴더를 미리 씀 — 도우미로 써도 같은 검사
    early = HC_HELPERS.replace(
        "    write_payment_events(ws, [], date='2024-06-01')\n",
        "    write_payment_events(ws, [], date='2024-06-01')\n    write_payment_events(ws, [], date='2024-06-02')\n",
        1,
    )
    got = future_input_days(_case(early), c, "숨은 테스트")
    assert len(got) == 1 and "2024-06-02(실행 2024-06-01 전)" in got[0]
    # 날짜 폴더를 쓰고 그 날짜로 실행하지 않음
    skipped = HC_HELPERS.replace(
        "    assert run_pipeline_run(date='2024-06-01', raw=ws.path('raw'), out=ws.path('out')).ok\n",
        "",
        1,
    )
    got = unrun_input_days(_case(skipped), c, "숨은 테스트")
    assert len(got) == 1 and "2024-06-01" in got[0]


def test_helper_folders_are_workspace_paths_but_commands_need_ws_path() -> None:
    c = _fitted()
    helper = render_contract_case(c, _plan(), DE.manifest)
    harness = DE.hidden / "harness"
    # 도우미에 넘기는 상대 폴더('raw')는 맞다 — 오탐하지 않는다
    ok = HC_HELPERS.replace("date='2024-06-01')\n", "date='2024-06-01', raw='raw')\n", 1)
    assert unknown_py_keywords(_case(ok), harness, helper, "숨은 테스트") == []
    # 명령에 그 폴더 이름을 문자열 그대로 넘기면 저장소 루트 기준 — 도우미 기본값(raw)으로 작업 폴더 이름을 안다
    bad = HC_HELPERS.replace("raw=ws.path('raw')", "raw='raw'", 1)
    got = unknown_py_keywords(_case(bad), harness, helper, "숨은 테스트")
    assert len(got) == 1 and "run_pipeline_run(raw='raw')" in got[0]
    # 도우미를 시그니처와 다르게 부름
    wrong = HC_HELPERS.replace(
        "write_payment_events(ws, [], date='2024-06-01')",
        "write_payment_events(ws, [], day='2024-06-01')",
        1,
    )
    got = unknown_py_keywords(_case(wrong), harness, helper, "숨은 테스트")
    assert len(got) == 1 and "write_payment_events(day=…)" in got[0]


def test_template_documents_the_helpers() -> None:
    """v3 부터 하네스 문서가 도우미를 먼저 쓴다. v4 는 예시를 재료와 무관한 모양으로 (재료 예시는 재료 숨은 안내 — #256 재료 옮기기)."""
    assert DE.manifest.ref == "data/python-duckdb@v4"
    harness_doc = (DE.hidden / "HARNESS.md").read_text("utf-8")
    public_doc = (DE.hidden / "PUBLIC.md").read_text("utf-8")
    assert "write_<입력>(ws, 행들" in harness_doc and "read_<출력>(ws" in harness_doc
    assert "read_daily_totals(ws)" in harness_doc and "write_orders(ws" in harness_doc
    assert "write_<입력>(ws, 행들" in public_doc and "read_daily_totals(ws)" in public_doc


def test_odd_locations_still_compile_or_get_no_helper() -> None:
    """리뷰 2026-11-24: 마디·`*` 없는 고정 위치는 키워드 인자 없이, 가운데 마디의 `*`·고정 인자와 같은 옵션 이름은 도우미 없이,
    따옴표·역슬래시가 든 요약도 독스트링이 깨지지 않게."""
    src = _helper(_with(_fitted(), f1={"path": "input/payments.jsonl"}))
    assert "def write_payment_events(ws: Workspace, rows: Iterable[PaymentEvent]) -> Path:" in src
    assert 'ws.write_jsonl(f"input/payments.jsonl", list(rows))' in src
    assert "def write_" not in _helper(_with(_fitted(), f1={"path": "{raw}/*/x.jsonl"}))
    assert "def write_" not in _helper(_with(_fitted(), f1={"path": "{rows}/{date}/*.jsonl"}))
    src = _helper(
        _with(_fitted(), f1={"path": "{raw}/{raw}/*.jsonl"})
    )  # 같은 마디 두 번 — 인자는 하나
    assert src.count("raw: str =") == 1
    src = _helper(_with(_fitted(), f1={"summary": 'a """ b \\ c'}))
    assert 'a \\"\\"\\" b' in src


def test_without_an_interface_llm_written_file_roles_are_cleared() -> None:
    """파일 역할 칸은 코드만 채운다 — 인터페이스 없는 재료에서 LLM 이 냈으면 비워 도우미가 생기지 않게."""
    from task_spec.starter.interface import fit_interface

    fitted = _fitted()
    out = PlanContractOutput(contract=fitted, hidden_plan=_plan())
    same, notes = fit_interface(out, None, [], [])
    assert notes == [] and all(f.role is None and f.entity is None for f in same.contract.fixtures)
    plain = PlanContractOutput(contract=_plain_contract(), hidden_plan=_plan())
    assert fit_interface(plain, None, [], [])[0] is plain  # 칸이 비어 있으면 그대로
