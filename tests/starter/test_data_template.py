"""데이터 엔지니어 템플릿(data/python-duckdb)을 파이프라인에 연결 (2026-11-09) — 템플릿 선택·Python 계약 도우미·CLI 계약 글·정적 검사. 가상 데이터만."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from task_spec.schemas import FeatureSeed, GeneratedFile, HiddenPlan, TaskContract
from task_spec.spec.incidents import incident_by_id, load_incidents
from task_spec.starter.contract import _file_fixtures, _needs
from task_spec.starter.hidden_helpers import render_contract_case, render_persistence_check
from task_spec.starter.py_helpers import command_name, flag, py_type
from task_spec.starter.render import contract_markdown, render_params
from task_spec.starter.static_check import (
    Overlay,
    future_input_days,
    py_harness_members,
    static_check,
    unknown_py_harness_members,
    unknown_py_keywords,
    unreadable_as_pass,
    unrun_input_days,
)
from task_spec.starter.templates import load_template, select_template
from tests.starter.test_contract import _spec

DE = load_template("data/python-duckdb")
CASES = "grading/cases"


def _f(name: str, typ: str, required: bool = True) -> dict[str, Any]:
    return {"name": name, "type": typ, "required": required, "refs": ["r1"]}


def _contract() -> TaskContract:
    return TaskContract.model_validate(
        {
            "interface_kind": "cli",
            "endpoints": [
                {
                    "endpoint_id": "e1",
                    "path": "pipeline/run.py",
                    "summary": "날짜 D 요약 배치",
                    "request": [
                        _f("date", "date"),
                        _f("raw_dir", "string"),
                        _f("out", "string"),
                        _f("lookback_days", "integer", False),
                    ],
                    "responses": [
                        {"status": 0, "description": "성공"},
                        {"status": 1, "description": "실패"},
                    ],
                    "refs": ["r1", "p1"],
                }
            ],
            "entities": [
                {
                    "entity_id": "m1",
                    "name": "Payment",
                    "fields": [
                        _f("payment_id", "string"),
                        _f("amount", "integer"),
                        _f("method", "enum(card|cash)"),
                        _f("note", "string", False),
                    ],
                }
            ],
            "fixtures": [
                {
                    "fixture_id": "f1",
                    "kind": "file",
                    "summary": "<raw>/YYYY-MM-DD/*.jsonl",
                    "path": "{raw_dir}/{date}/*.jsonl",
                    "refs": ["r1"],
                }
            ],
            "public_tests": [
                {
                    "test_id": "pt1",
                    "ac_id": "p1",
                    "checks": "날짜 순서대로 실행하면 요약이 정의대로",
                }
            ],
            "covers": {"r1": ["e1", "m1"]},
        }
    )


def _plan() -> HiddenPlan:
    return HiddenPlan.model_validate(
        {
            "harness": {},
            "cases": [
                {
                    "case_id": "hc1",
                    "ac_id": "h1",
                    "trap_id": "t1",
                    "scenario": "다음 날 폴더에 앞 날짜 결제",
                    "expect": "앞 날짜 요약에 반영",
                    "intended_reason": "late_data_missed",
                }
            ],
        }
    )


def test_data_engineer_template_is_selected_and_python_only_artifacts() -> None:
    assert select_template("data.eng").manifest.ref == "data/python-duckdb@v1"  # type: ignore[union-attr]
    assert render_persistence_check(_contract(), DE.manifest) is None
    p = render_params(_plan(), DE.manifest)
    assert p.path == "grading/grader-params.json"
    assert DE.manifest.prompt_tasks["gen_hidden"] == "gen_hidden_py"


def test_python_contract_helper_runs_cli_commands_with_contract_options() -> None:
    hidden = render_contract_case(_contract(), _plan(), DE.manifest, audience="hidden")
    public = render_contract_case(_contract(), _plan(), DE.manifest, audience="public")
    assert hidden is not None and public is not None
    assert hidden.path == f"{CASES}/contract.py" and public.path == "public_tests/contract.py"
    c = hidden.content
    assert "from harness.grader import JobResult, run" in c
    assert (
        "class Payment(TypedDict):" in c
        and "method: Literal['card', 'cash']" in c
        and "note: NotRequired[str]" in c
    )
    assert (
        "def run_pipeline_run(" in c
        and "lookback_days: str | int | float | os.PathLike[str] | None = None," in c
    )
    assert "args += ['--raw-dir'," in c and "if lookback_days is not None:" in c
    assert (
        "return run('pipeline/run.py', *args, crash_after_bytes=crash_after_bytes, timeout=timeout)"
        in c
    )
    compile(c, "contract.py", "exec")  # 문법이 맞다
    assert (
        command_name("pipeline/jobs/daily.py") == "run_pipeline_jobs_daily"
        and flag("raw_dir") == "--raw-dir"
    )
    assert (
        py_type("integer[]", {}) == "list[int]"
        and py_type("Payment", {"Payment": "Payment"}) == "Payment"
    )


def test_cli_contract_text_shows_options_as_flags() -> None:
    md = contract_markdown(_contract())
    assert "### `pipeline/run.py`" in md
    assert (
        "- 옵션: `--date` (date) · `--raw-dir` (string) · `--out` (string) · `--lookback-days` (integer, 선택)"
        in md
    )
    assert "- 본문:" not in md and "- `0` 성공" in md


HC1 = (
    '"""hc1 늦게 들어온 앞 날짜 결제"""\n'
    "from harness.grader import reason, workspace\n"
    "from cases.contract import run_pipeline_run\n\n\n"
    "def test_hc1_late() -> None:\n"
    "    ws = workspace()\n"
    "    r = run_pipeline_run(date='2026-09-01', raw_dir=ws.path('raw'), out=ws.path('out'))\n"
    "    assert r.ok, reason('late_data_missed', '앞 날짜 빠짐')\n"
)


def _run(hidden: str = HC1, starter_extra: str = "") -> list[str]:
    starter = Overlay(
        kind="starter",
        files=(GeneratedFile(path="pipeline/job.py", content=starter_extra or "X = 1\n"),),
    )
    hid = Overlay(
        kind="hidden",
        files=(
            GeneratedFile(path=f"{CASES}/test_hc1_late.py", content=hidden),
            render_params(_plan(), DE.manifest),
        ),
    )
    res = static_check(
        [starter, hid], template=DE, plan=_plan(), spec=_spec(), contract=_contract(), seed=None
    )
    return [p.message for p in res.problems]


def test_python_overlays_pass_and_rules_bite() -> None:
    assert _run() == []
    assert any(
        "지원자 구현 내부" in m
        for m in _run(
            HC1.replace("from harness.grader", "from pipeline import job\nfrom harness.grader")
        )
    )
    assert any("템플릿에 없는 라이브러리" in m for m in _run(starter_extra="import polars as pl\n"))
    assert not any(
        "템플릿에 없는 라이브러리" in m for m in _run(starter_extra="import pandas as pd\n")
    )  # 2026-11-09 템플릿에 넣음
    assert any(
        "템플릿에 없는 라이브러리" in m
        for m in _run(starter_extra="from harness.grader import run\n")
    )
    assert any("not_a_reason" in m for m in _run(HC1.replace("late_data_missed", "not_a_reason")))


def _with_fixture(
    summary: str,
    request: list[dict[str, Any]] | None = None,
    path: str | None = "{raw_dir}/{date}/*.jsonl",
) -> TaskContract:
    d = _contract().model_dump()
    d["fixtures"][0]["summary"] = summary
    d["fixtures"][0]["path"] = path
    if request is not None:
        d["endpoints"][0]["request"] = request
    return TaskContract.model_validate(d)


def _batch_seed() -> FeatureSeed:
    inc = incident_by_id(
        load_incidents(Path("incidents/public_v1")), "pub:data.eng:batch-rerun-late-partial"
    )
    assert inc is not None and inc.feature is not None
    return inc.feature


def test_file_fixture_needs_one_format_and_a_row_entity() -> None:
    assert _file_fixtures(_with_fixture("<raw>/YYYY-MM-DD/*.jsonl — 한 줄에 Payment 하나")) == []
    two = _file_fixtures(_with_fixture("<raw>/YYYY-MM-DD/ 의 csv 또는 parquet, 한 행에 Payment"))
    assert len(two) == 1 and "csv·parquet" in two[0]
    nameless = _file_fixtures(_with_fixture("<raw>/YYYY-MM-DD/*.jsonl — 결제번호·금액 등"))
    assert len(nameless) == 1 and "행 모양 엔티티" in nameless[0]
    # 위치는 칸(path)으로 — 없거나 명령 옵션이 아닌 자리표시자면 재요청 (#187 woori001)
    no_path = _file_fixtures(_with_fixture("한 줄에 Payment 하나 (jsonl)", path=None))
    assert len(no_path) == 1 and "위치(path)가 없음" in no_path[0]
    stray = _file_fixtures(
        _with_fixture("한 줄에 Payment 하나 (jsonl)", path="{raw}/{YYYY-MM-DD}/*.jsonl")
    )
    assert len(stray) == 1 and "raw, YYYY-MM-DD 가 명령 옵션이 아님" in stray[0]


def test_dated_input_folders_need_date_named_input_folder_and_date_option() -> None:
    """날짜별 입력 폴더 = 파일 입력 환경의 위치(path) 마디 하나가 통째로 날짜 형식 명령 옵션. 글 표기를 짐작하지 않는다 (#187 woori001:
    `{YYYY-MM-DD}/` 로 바르게 적은 계약을 `YYYY-MM-DD/` 꼴만 받던 검사가 재요청)."""
    seed = _batch_seed()

    def dated(c: TaskContract) -> list[str]:
        return [p for p in _needs(c, _plan(), seed) if "날짜별 입력 폴더" in p]

    assert not dated(
        _with_fixture("한 줄에 Payment 하나")
    )  # {raw_dir}/{date}/*.jsonl, date 는 type date
    as_of = [_f("as_of", "date"), _f("raw_dir", "string"), _f("out", "string")]
    assert not dated(
        _with_fixture("Payment", request=as_of, path="{raw_dir}/{as_of}/*.jsonl")
    )  # 이름에 date 가 없어도
    # 출력 파티션 date=… 만 있고 입력은 파일 하나 — 2026-11-09 데이터 알파
    assert dated(_with_fixture("Payment", path="{raw_dir}/payments.jsonl"))
    assert dated(
        _with_fixture("Payment", path="{out}/date={date}/part.parquet")
    )  # 마디가 통째로 옵션이 아니다
    # 마디로 쓴 옵션이 글자 형식이면 날짜로 치지 않고, 무엇을 고칠지 짚는다 (woori001: run_date string + 'YYYY-MM-DD 형식')
    texty = [_f("run_date", "string"), _f("raw_dir", "string"), _f("out", "string")]
    [msg] = dated(_with_fixture("Payment", request=texty, path="{raw_dir}/{run_date}/*.jsonl"))
    assert "run_date 의 type 이 날짜가 아님" in msg


PT_BAD = (
    "from harness.grader import workspace\n"
    "from contract import run_pipeline_run\n\n\n"
    "def test_pt1() -> None:\n"
    "    ws = workspace()\n"
    "    ws.write_jsonl('raw/2026-09-01/a.jsonl', [])\n"
    "    r = run_pipeline_run(date='2026-09-01', raw_dir=ws.input, out=ws.path('out'))\n"
    "    assert r.returncode == 0 and r.ok\n"
    "    ws._root\n"
)


def test_python_tests_may_only_use_documented_harness_members() -> None:
    harness = DE.hidden / "harness"
    members = py_harness_members(harness / "grader.py")
    assert {"path", "write_jsonl", "read_parquet", "partitions"} <= members["Workspace"]
    assert {"ok", "crashed", "code", "stdout"} <= members["JobResult"]
    assert not any(n.startswith("_") for ns in members.values() for n in ns)
    got = unknown_py_harness_members(
        [GeneratedFile(path="public_tests/test_pt1.py", content=PT_BAD)], harness, "공개 테스트"
    )
    assert len(got) == 1
    assert "ws.input (Workspace" in got[0] and "r.returncode (JobResult" in got[0]
    assert "ws._root" in got[0] and "ws.write_jsonl" not in got[0]
    assert unknown_py_harness_members([GeneratedFile(path="x.py", content=HC1)], harness, "x") == []


PT_KW = (
    "from harness.grader import params, workspace\n"
    "from contract import run_pipeline_run as job\n\n\n"
    "def test_pt1() -> None:\n"
    "    ws = workspace()\n"
    "    ws.write_csv('raw/2026-09-01/a.csv', [{'a': 1}], header=True)\n"
    "    ws.write_csv('raw/2026-09-01/b.csv', [{'a': 1}], columns=['a'])\n"
    "    r = job(date='2026-09-01', raw=ws.path('raw'), out=ws.path('out'))\n"
    "    assert r.ok, r.last_error()\n"
    "    assert params.int('n', fallback=1) == 1 and params.int('n', default=1)\n"
)


def test_python_tests_may_only_use_keywords_the_harness_and_helper_accept() -> None:
    helper = render_contract_case(_contract(), _plan(), DE.manifest, audience="public")
    got = unknown_py_keywords(
        [GeneratedFile(path="public_tests/test_pt1.py", content=PT_KW)],
        DE.hidden / "harness",
        helper,
        "공개 테스트",
    )
    assert len(got) == 1
    msg = got[0]
    assert "ws.write_csv(header=…)" in msg and "columns" in msg  # 받는 이름을 알려 준다
    assert "job(raw=…)" in msg and "raw_dir" in msg  # 별칭으로 import 한 계약 함수
    assert "params.int(default=…)" in msg  # 모듈 수준 인스턴스의 메서드
    assert "columns=" not in msg and "fallback=" not in msg
    ok = (
        PT_KW.replace(", header=True", "")
        .replace("raw=", "raw_dir=")
        .replace("default=", "fallback=")
    )
    assert (
        unknown_py_keywords(
            [GeneratedFile(path="public_tests/test_pt1.py", content=ok)],
            DE.hidden / "harness",
            helper,
            "공개 테스트",
        )
        == []
    )


def test_python_tests_must_not_use_with_or_skip_required_harness_arguments() -> None:
    src = (
        "from harness.grader import workspace\n\n\n"
        "def test_pt2() -> None:\n"
        "    with workspace() as ws:\n"
        "        ws.write_jsonl('raw/2026-09-01/a.jsonl', [])\n"
        "        assert ws.partitions('out') == []\n"
        "        assert ws.partitions('out', 'date') == []\n"
    )
    got = unknown_py_keywords(
        [GeneratedFile(path="public_tests/test_pt2.py", content=src)],
        DE.hidden / "harness",
        None,
        "공개 테스트",
    )
    assert len(got) == 1
    assert "with workspace() — Workspace 는 with 로 쓰지 않는다" in got[0]
    assert "ws.partitions(…) 에 필수 인자 key 가 빠짐" in got[0]  # with 로 얻은 이름도 추적
    assert got[0].count("partitions") == 1  # 위치 인자로 key 를 준 호출은 통과


def test_python_tests_must_pass_workspace_paths_not_bare_strings() -> None:
    """2026-11-09 데이터 알파 5회차: run_…(raw_dir="raw") — 명령은 저장소 루트에서 돌아 모든 구현이 '입력 폴더 없음'."""
    helper = render_contract_case(_contract(), _plan(), DE.manifest, audience="public")
    src = (
        "from harness.grader import workspace\n"
        "from public_tests.contract import run_pipeline_run\n\n\n"
        "def test_pt1() -> None:\n"
        "    ws = workspace()\n"
        "    ws.write_csv('raw/2026-09-01/a.csv', [{'a': 1}])\n"
        "    r = run_pipeline_run(date='2026-09-01', raw_dir='raw', out='out/x')\n"
        "    assert ws.read_parquet('out/x/date=2026-09-01')\n"
    )
    got = unknown_py_keywords(
        [GeneratedFile(path="public_tests/test_pt1.py", content=src)],
        DE.hidden / "harness",
        helper,
        "공개 테스트",
    )
    assert len(got) == 1
    assert "run_pipeline_run(raw_dir='raw')" in got[0] and "ws.path('raw')" in got[0]
    assert "run_pipeline_run(out='out/x')" in got[0]
    assert "date=" not in got[0]  # 날짜 같은 값은 경로가 아니다
    fixed = src.replace("raw_dir='raw'", "raw_dir=ws.path('raw')").replace(
        "out='out/x'", "out=ws.path('out/x')"
    )
    assert (
        unknown_py_keywords(
            [GeneratedFile(path="public_tests/test_pt1.py", content=fixed)],
            DE.hidden / "harness",
            helper,
            "공개 테스트",
        )
        == []
    )


def test_duckdb_copy_options_with_equals_are_caught_before_running() -> None:
    """2026-11-09 데이터 알파 2·6회차: 참조 구현이 COPY … (HEADER, AUTO_DETECT=TRUE) 로 Parser Error — manifest code_pitfalls."""
    bad = (
        "import duckdb\n\n\n"
        "def load(con, path):\n"
        "    con.execute(f\"COPY raw FROM '{path}' (HEADER, AUTO_DETECT=TRUE)\")\n"
        '    con.execute("COPY (SELECT * FROM raw) TO ? (FORMAT parquet)", [str(path)])\n'
    )
    ref = Overlay(
        kind="ref_accepted",
        files=(GeneratedFile(path="pipeline/load.py", content=bad),),
        label="d1",
    )
    msgs = [
        p.message
        for p in static_check(
            [ref], template=DE, plan=_plan(), spec=_spec(), contract=_contract(), seed=None
        ).problems
    ]
    hit = [m for m in msgs if "되풀이되는 실수" in m]
    assert len(hit) == 1
    assert hit[0].startswith("ref_accepted(d1)") and "pipeline/load.py:5" in hit[0]  # 라벨·줄
    assert "FORMAT parquet" not in hit[0].split(" — ")[0]  # 바른 COPY 는 걸지 않는다
    fixed = bad.replace("(HEADER, AUTO_DETECT=TRUE)", "(HEADER, DELIMITER ',')")
    ok = Overlay(
        kind="ref_accepted",
        files=(GeneratedFile(path="pipeline/load.py", content=fixed),),
        label="d1",
    )
    assert not [
        p
        for p in static_check(
            [ok], template=DE, plan=_plan(), spec=_spec(), contract=_contract(), seed=None
        ).problems
        if "되풀이되는 실수" in p.message
    ]


def test_stack_notes_reach_every_generator_and_api_pitfalls_are_caught() -> None:
    """2026-11-09 데이터 알파 7회차: 허용 구현이 DuckDB·pyarrow API 를 틀림 (TIMESTAMP 에 substr, .arrow() 표 아님). pandas·numpy 는 템플릿에 넣음."""
    from task_spec.nodes.starter.generate import stack_text

    text = stack_text(DE.manifest)
    assert "이 스택에서 쓰는 법:" in text and "to_arrow_table()" in text and "all_varchar" in text
    assert "pandas 2.3" in text and ".df()" in text
    java = load_template("backend/java-spring-boot3-jpa")
    assert "이 스택에서 쓰는 법" not in stack_text(java.manifest)  # 다른 템플릿은 그대로
    code = "def f(con):\n    t = con.execute('SELECT 1').arrow()\n    return con.execute('SELECT 1').df()\n"
    ref = Overlay(
        kind="ref_naive",
        files=(GeneratedFile(path="pipeline/run.py", content=code),),
        label="naive-all",
    )
    msgs = [
        p.message
        for p in static_check(
            [ref], template=DE, plan=_plan(), spec=_spec(), contract=_contract(), seed=None
        ).problems
        if "되풀이되는 실수" in p.message
    ]
    assert len(msgs) == 1 and msgs[0].startswith(
        "ref_naive(naive-all)"
    )  # .df() 는 이제 된다 (pandas)
    assert any("pipeline/run.py:2" in m and "RecordBatchReader" in m for m in msgs)


def test_tests_that_skip_a_written_day_are_sent_back() -> None:
    """2026-11-09 재검증: 날짜 폴더를 써 두고 마지막 날짜로 한 번만 실행 — '이번 폴더에 나온 날짜만 다시 계산' 허용 설계가 떨어진다."""
    skip = (
        "from harness.grader import workspace\n"
        "from cases.contract import run_pipeline_run\n\n\n"
        "def test_hc4_rerun() -> None:\n"
        "    ws = workspace()\n"
        "    ws.write_csv('raw/2026-09-01/batch-0001.csv', [{'a': 1}])\n"
        "    ws.write_csv('raw/2026-09-02/batch-0001.csv', [{'a': 2}])\n"
        "    assert run_pipeline_run(date='2026-09-02', raw_dir=ws.path('raw'), out=ws.path('out')).ok\n"
    )
    got = unrun_input_days(
        [GeneratedFile(path="grading/cases/test_hc4.py", content=skip)], _contract(), "숨은 테스트"
    )
    assert len(got) == 1 and "2026-09-01" in got[0] and "test_hc4_rerun" in got[0]
    daily = skip.replace(
        "    assert run_pipeline_run(date='2026-09-02'",
        "    assert run_pipeline_run(date='2026-09-01', raw_dir=ws.path('raw'), out=ws.path('out')).ok\n"
        "    assert run_pipeline_run(date='2026-09-02'",
    )
    assert (
        unrun_input_days(
            [GeneratedFile(path="grading/cases/test_hc4.py", content=daily)], _contract(), "x"
        )
        == []
    )
    looped = skip.replace("'raw/2026-09-01/batch-0001.csv'", "f'raw/{day}/batch-0001.csv'")
    assert (
        unrun_input_days([GeneratedFile(path="t.py", content=looped)], _contract(), "x") == []
    )  # 변수 경로는 보지 않음


def test_run_day_comes_from_contract_date_options() -> None:
    """#178: 실행 날짜는 옵션 이름에 'date' 가 들었는가가 아니라 계약 형식(날짜·날짜-시각)으로 정한다 — `as_of=` 도 실행 날짜다."""
    c = _contract()
    e = c.endpoints[0]
    as_of = e.model_copy(
        update={
            "request": [
                f.model_copy(update={"name": "as_of"}) if f.name == "date" else f for f in e.request
            ]
        }
    )
    renamed = c.model_copy(update={"endpoints": [as_of]})
    skip = (
        "from cases.contract import run_pipeline_run\n\n\n"
        "def test_hc4(ws) -> None:\n"
        "    ws.write_csv('raw/2026-09-01/batch-0001.csv', [{'a': 1}])\n"
        "    ws.write_csv('raw/2026-09-02/batch-0001.csv', [{'a': 2}])\n"
        "    assert run_pipeline_run(as_of='2026-09-02', raw_dir=ws.path('raw'), out=ws.path('out')).ok\n"
    )
    case = [GeneratedFile(path="t.py", content=skip)]
    got = unrun_input_days(case, renamed, "x")
    assert len(got) == 1 and "2026-09-01" in got[0]
    # 날짜 옵션이 아닌 칸의 글자 날짜(예: 출력 폴더 이름)는 실행 날짜가 아니다
    other = skip.replace("out=ws.path('out')", "out='2026-09-01'")
    assert len(unrun_input_days([GeneratedFile(path="t.py", content=other)], renamed, "x")) == 1
    # 날짜 옵션에 변수를 넘기면 판정하지 않는다
    var = skip.replace("as_of='2026-09-02'", "as_of=day")
    assert unrun_input_days([GeneratedFile(path="t.py", content=var)], renamed, "x") == []


def test_code_pitfall_unless_and_copy_parameter_order() -> None:
    """.arrow().read_all() 은 바른 꼴(7회차 흔한 구현) · COPY (… ?) TO ? 는 TO 의 ? 가 먼저 묶임(1.5.6 에서 확인, 4회차 허용 구현)."""

    def hits(code: str) -> list[str]:
        ref = Overlay(
            kind="ref_accepted",
            files=(GeneratedFile(path="pipeline/run.py", content=code),),
            label="d1",
        )
        return [
            p.message
            for p in static_check(
                [ref], template=DE, plan=_plan(), spec=_spec(), contract=_contract(), seed=None
            ).problems
            if "되풀이되는 실수" in p.message
        ]

    assert hits("t = con.execute(q).arrow().read_all()\n") == []
    assert len(hits("t = con.execute(q).arrow()\nn = t.num_rows\n")) == 1
    bad = "con.execute('''COPY (SELECT * FROM agg WHERE d = ?\n ORDER BY s) TO ? (FORMAT parquet)''', [d, p])\n"
    assert any("번호 매개변수" in m for m in hits(bad))
    assert (
        hits(
            "con.execute('COPY (SELECT * FROM agg WHERE d = $1) TO $2 (FORMAT parquet)', [d, p])\n"
        )
        == []
    )
    assert hits("con.execute('COPY (SELECT * FROM agg) TO ? (FORMAT parquet)', [p])\n") == []


def test_crash_cases_must_not_treat_unreadable_output_as_pass() -> None:
    """2026-11-10 재검증: 9회 중 6회 크래시 케이스가 '읽을 수 없음'이면 단언을 건너뜀 — 최종 위치에 바로 쓰다 죽는 흔한 구현이 통과."""
    skip = (
        "def read(ws):\n"
        "    try:\n"
        "        return ws.read_parquet('out')\n"
        "    except Exception:\n"
        "        return 'unreadable'\n\n\n"
        "def test_hc3() -> None:\n"
        "    v = read(None)\n"
        "    if v != 'unreadable':\n"
        "        assert v == [], 'x'\n"
    )
    either = skip.replace(
        "    if v != 'unreadable':\n        assert v == [], 'x'\n",
        "    assert v == 'unreadable' or v == [], 'x'\n",
    )
    strict = skip.replace(
        "    if v != 'unreadable':\n        assert v == [], 'x'\n",
        "    assert v == [], 'x'  # 읽을 수 없음이면 다른 값이라 실패\n",
    )

    def got(src: str) -> list[str]:
        return unreadable_as_pass(
            [GeneratedFile(path="grading/cases/test_hc3.py", content=src)], "숨은 테스트"
        )

    assert len(got(skip)) == 1 and "'unreadable'" in got(skip)[0]
    assert len(got(either)) == 1
    assert got(strict) == []


def test_tests_must_not_write_later_day_folders_before_running() -> None:
    """2026-11-10 알파 10회차: 9/2 폴더를 9/1 실행 전에 써 두어, 받은 폴더를 모두 읽는 허용 구현이 떨어짐 (손 구현으로 재현)."""
    early = (
        "from harness.grader import workspace\n"
        "from cases.contract import run_pipeline_run\n\n\n"
        "def test_hc2() -> None:\n"
        "    ws = workspace()\n"
        "    ws.write_csv('raw/2026-09-01/batch-01.csv', [{'a': 1}])\n"
        "    ws.write_csv('raw/2026-09-02/batch-01.csv', [{'a': 1}])\n"
        "    assert run_pipeline_run(date='2026-09-01', raw_dir=ws.path('raw'), out=ws.path('out')).ok\n"
        "    assert run_pipeline_run(date='2026-09-02', raw_dir=ws.path('raw'), out=ws.path('out')).ok\n"
    )
    got = future_input_days(
        [GeneratedFile(path="grading/cases/test_hc2.py", content=early)], _contract(), "숨은 테스트"
    )
    assert len(got) == 1 and "2026-09-02(실행 2026-09-01 전)" in got[0]
    in_order = early.replace(
        "    ws.write_csv('raw/2026-09-02/batch-01.csv', [{'a': 1}])\n    assert run_pipeline_run(date='2026-09-01'",
        "    assert run_pipeline_run(date='2026-09-01'",
    ).replace(
        "    assert run_pipeline_run(date='2026-09-02'",
        "    ws.write_csv('raw/2026-09-02/batch-01.csv', [{'a': 1}])\n    assert run_pipeline_run(date='2026-09-02'",
    )
    assert future_input_days([GeneratedFile(path="t.py", content=in_order)], _contract(), "x") == []


def test_read_csv_arguments_in_copy_options_are_caught() -> None:
    """2026-11-10 알파 11회차: COPY … FROM ? (…, ALL_VARCHAR true) — 1.5.6 'Unrecognized option' (stack_notes 가 표 함수라고 안 적었다)."""

    def hits(code: str) -> list[str]:
        ref = Overlay(
            kind="ref_accepted",
            files=(GeneratedFile(path="pipeline/run.py", content=code),),
            label="d1",
        )
        return [
            p.message
            for p in static_check(
                [ref], template=DE, plan=_plan(), spec=_spec(), contract=_contract(), seed=None
            ).problems
            if "되풀이되는 실수" in p.message
        ]

    assert any(
        "read_csv" in m
        for m in hits(
            "con.execute(\"COPY t FROM ? (HEADER, DELIMITER ',', ALL_VARCHAR true)\", [p])\n"
        )
    )
    assert (
        hits('con.execute("SELECT * FROM read_csv(?, header = true, all_varchar = true)", [p])\n')
        == []
    )


def test_command_path_must_be_where_the_starter_can_write() -> None:
    """2026-11-10 알파 12회차: 계약이 batch/daily_sales_summary.py 로 정해 첫 답 통과 → 시작 코드가 '쓸 수 없는 경로'로 실패."""
    from task_spec.starter.contract import _command_paths

    assert _command_paths(_contract(), DE.manifest) == []  # pipeline/run.py
    d = _contract().model_dump()
    d["endpoints"][0]["path"] = "batch/daily_sales_summary.py"
    got = _command_paths(TaskContract.model_validate(d), DE.manifest)
    assert len(got) == 1 and "pipeline/**/*.py" in got[0] and "pipeline/run.py" in got[0]


def test_read_json_invalid_arguments_are_caught() -> None:
    """2026-11-10 알파 13회차: read_json_auto(…, format='json', lines=true) → 'lines' 없음, 다시 만들자 format='json' → 허용 값 아님 (1.5.6 확인)."""

    def hits(code: str) -> list[str]:
        ref = Overlay(
            kind="ref_accepted",
            files=(GeneratedFile(path="pipeline/run.py", content=code),),
            label="d1",
        )
        return [
            p.message
            for p in static_check(
                [ref], template=DE, plan=_plan(), spec=_spec(), contract=_contract(), seed=None
            ).problems
            if "되풀이되는 실수" in p.message
        ]

    assert any(
        "lines" in m
        for m in hits("q = \"SELECT * FROM read_json_auto($files, format='json', lines=true)\"\n")
    )
    assert any(
        "'json' 은 없다" in m
        for m in hits("q = \"SELECT * FROM read_json($files, format='json')\"\n")
    )
    assert hits("q = \"SELECT * FROM read_json($files, format = 'newline_delimited')\"\n") == []
    assert hits('q = "SELECT * FROM read_json_auto($files)"\n') == []


def test_command_script_must_fix_sys_path_before_importing_its_package() -> None:
    """2026-11-10 알파 14회차: 허용 구현이 맨 위에서 `from pipeline.inputs import …` — `python pipeline/run.py` 로 ModuleNotFoundError."""
    from task_spec.starter.static_check import _script_imports

    bad = "import sys\nfrom pipeline.inputs import read_rows\nsys.path.insert(0, '..')\n"
    good = "import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).resolve().parent.parent))\nfrom pipeline.inputs import read_rows\n"

    def got(code: str, path: str = "pipeline/run.py") -> list[str]:
        o = Overlay(
            kind="ref_accepted", label="d1", files=(GeneratedFile(path=path, content=code),)
        )
        return [p.message for p in _script_imports(o, DE, _contract())]

    assert len(got(bad)) == 1 and "pipeline/run.py:2" in got(bad)[0]
    assert got(good) == []
    assert got(bad, path="pipeline/helpers.py") == []  # 명령 스크립트가 아니면 import 로 쓰인다


def test_unreadable_marker_inside_a_tuple_is_also_caught() -> None:
    """2026-11-10 알파 14회차: `assert v in ("<unreadable>", {})` — == 비교만 보던 검사가 놓쳤다."""
    src = (
        "def read(ws):\n    try:\n        return ws.read_parquet('out')\n    except Exception:\n        return '<unreadable>'\n\n\n"
        "def test_hc3() -> None:\n    v = read(None)\n    assert v in ('<unreadable>', {}), 'x'\n"
    )
    assert (
        len(
            unreadable_as_pass(
                [GeneratedFile(path="grading/cases/test_hc3.py", content=src)], "숨은 테스트"
            )
        )
        == 1
    )
    ok = src.replace(
        "assert v in ('<unreadable>', {}), 'x'",
        "assert v not in ('<unreadable>',) and v == {}, 'x'",
    )
    assert unreadable_as_pass([GeneratedFile(path="t.py", content=ok)], "x") == []
