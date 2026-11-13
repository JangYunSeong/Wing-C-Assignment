"""손 시범 도구 (eval/pilot, docs/design/analysis-stage.md §9.1) — 순수한 부분만. 가상 데이터."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eval.pilot import analyze as an
from eval.pilot import freeze
from eval.pilot.analyze import RunData
from eval.pilot.facilitator import (
    EMPTY_QUESTION,
    Entry,
    Session,
    client_script,
    handle,
    states,
)
from eval.pilot.grade import Method, SuiteRun, methods
from eval.pilot.llm_facilitator import Chooser, apply_choice, render_items
from eval.pilot.model import Fact, Material, parse_label, run_label, term_overlaps
from eval.pilot.simulate import classify, pooled, record_official
from eval.pilot.simulate import run as simulate_run
from eval.pilot.validity import judge
from task_spec.schemas import AskedItem, FacilitatorChooseOutput
from task_spec.starter.answer_rule import NO_INFO as NO
from task_spec.starter.answer_rule import ONE_AT_A_TIME, Reply
from task_spec.starter.facilitator import Answer
from task_spec.starter.facilitator import Material as Doc

T = "2026-11-13T10:00:00+09:00"
F1 = Answer(
    id="f1",
    topic="회원별 발급 제한",
    match_terms=["회원당"],
    answer="회원당 한 번이에요.",
    answer_hint=None,
)
F2 = Answer(
    id="f2",
    topic="이벤트 쿠폰 수량",
    match_terms=["수량"],
    answer="정해 둔 수량만큼.",
    answer_hint="설정에 따라요.",
)
C1 = Answer(
    id="c1",
    topic="오픈 직후 접속량",
    match_terms=["요청량"],
    answer="평소의 수십 배.",
    answer_hint="'운영 지표' 자료에서.",
)
DOCS = [
    Doc(id="a1", title="운영 지표", content="가상 지표"),
    Doc(id="a2", title="회의록", content="가상 회의록"),
]


def _session(mode: str = "new", **kw: Any) -> Session:
    items = [C1] if mode == "legacy" else [F1, F2, C1]
    return Session(
        label="x-p1-opus-r1", token="0" * 32, mode=mode, items=items, materials=DOCS, **kw
    )  # type: ignore[arg-type]


def _ask(s: Session, log: list[Entry], kind: str, arg: str) -> str:
    text, entry = handle(s, log, kind, arg, now=T)  # type: ignore[arg-type]
    log.append(entry)
    return text


def test_new_mode_keeps_item_state_across_questions() -> None:
    s, log = _session(), []  # type: ignore[var-annotated]
    assert _ask(s, log, "question", "수량이랑 회원당은요?") == ONE_AT_A_TIME
    assert _ask(s, log, "question", "수량은요?") == F2.answer
    assert states(log)["f2"] == "open"
    assert _ask(s, log, "question", "수량 말고 회원당은요?") == F1.answer  # 열린 f2 는 세지 않음
    assert [e.reply.rule for e in log if e.reply] == [1, 2, 2]


def test_legacy_mode_counts_every_question_toward_the_limit() -> None:
    s, log = _session("legacy", max_questions=2, max_requests=1), []  # type: ignore[var-annotated]
    _ask(s, log, "question", "점심은요?")  # 정보 없음도 한도에 든다
    _ask(s, log, "question", "요청량은요?")
    assert "한도" in _ask(s, log, "question", "요청량 다시요?")
    assert sum(e.kind == "question" for e in log) == 2 and log[-1].kind == "refused"


def test_materials_list_serve_and_limit() -> None:
    s, log = _session("legacy", max_questions=2, max_requests=1), []  # type: ignore[var-annotated]
    assert "남은 요청 1개" in _ask(s, log, "material", "")
    assert "가상 지표" in _ask(s, log, "material", "1")
    assert "가상 지표" in _ask(s, log, "material", "1")  # 받은 자료를 다시 보는 것은 차감하지 않음
    assert "한도" in _ask(s, log, "material", "2")
    assert "번호로" in _ask(s, log, "material", "운영")
    unlimited, log2 = _session(), []  # type: ignore[var-annotated]
    _ask(unlimited, log2, "material", "1")
    assert "회의록" in _ask(unlimited, log2, "material", "2")


def test_empty_question_is_refused_and_client_has_only_address_and_token() -> None:
    s, log = _session(), []  # type: ignore[var-annotated]
    assert _ask(s, log, "question", "  ") == EMPTY_QUESTION
    script = client_script("ab" * 16, 9999)
    assert "ab" * 16 in script and "127.0.0.1:9999" in script and "answer_rule" not in script


def test_labels_round_trip() -> None:
    assert parse_label(run_label("kb005-nplus1", 3, "haiku", 2)) == ("kb005-nplus1", 3, "haiku", 2)
    with pytest.raises(ValueError):
        parse_label("kb005-p5-opus-r1")


def test_term_overlaps_flags_shared_words() -> None:
    a = Answer(id="f1", topic="t", match_terms=["발급 수량"], answer="a", answer_hint=None)
    b = Answer(id="c1", topic="t", match_terms=["수량"], answer="b", answer_hint=None)
    assert term_overlaps([a, b]) and not term_overlaps([a, C1])


def test_junit_methods_are_grouped_by_package(tmp_path: Path) -> None:
    (tmp_path / "TEST-x.xml").write_text(
        """<testsuite name="s">
<testcase classname="com.example.service.publictests.Pt1ListTest" name="a"/>
<testcase classname="com.example.service.publictests.Pt1ListTest" name="최신순정렬()"><failure message="expected 409"/></testcase>
<testcase classname="com.example.service.publictests.PersistenceCheckTest" name="saves"/>
<testcase classname="com.example.service.grading.cases.Hc1Test" name="h"/>
</testsuite>""",
        "utf-8",
    )
    ms = methods(tmp_path, {"Pt1ListTest": "pt1"}, {"Hc1Test": "hc1"})
    assert [(m.group, m.case, m.ok) for m in ms] == [
        ("public", "pt1", True),
        ("public", "pt1", False),
        ("persistence", "PersistenceCheckTest", True),
        ("hidden", "hc1", True),
    ]
    assert ms[1].method == "최신순정렬"  # Gradle 이 붙인 괄호를 뗀다 — method_req 키와 맞게


# ── 판정 ──


def _run(
    arm: int,
    model: str,
    run: int = 1,
    p1: float = 1.0,
    p2: float = 1.0,
    hidden: float = 0.0,
    **kw: Any,
) -> RunData:
    return RunData(
        label=run_label("m1", arm, model, run),
        mid="m1",
        arm=arm,
        model=model,
        run=run,
        p1=p1,
        p2=p2,
        hidden=hidden,
        setup=["ok", "ok"],
        **kw,
    )


def _base(p2_arm2: dict[str, float], p0: dict[str, float] | None = None) -> list[RunData]:
    p0 = p0 or {"opus": 1.0, "sonnet": 1.0, "haiku": 0.5}
    out = [_run(0, md, p2=v) for md, v in p0.items()]
    out += [_run(2, md, run=r, p2=v) for md, v in p2_arm2.items() for r in (1, 2)]
    return out


def test_material_state_gate_eligibility_and_reference() -> None:
    st = an.material_state("m1", _base({"opus": 0.5, "sonnet": 1.0, "haiku": 0.5}))
    assert (st.ra, st.gate_stop) == (True, False)
    assert st.eligible == ["opus"]  # sonnet 은 판 2 가 천장, haiku 는 판 0 이 낮아 정보의 값 0
    assert st.reference == "sonnet"  # haiku 의 판 0 은 ⅔ 미만
    assert (
        an.material_state("m1", _base({"opus": 1.0, "sonnet": 1.0, "haiku": 0.5})).gate_stop is True
    )


def test_verdict_na_needs_opus_gain_and_no_loss_elsewhere() -> None:
    runs = _base(
        {"opus": 0.5, "sonnet": 0.5, "haiku": 0.5}, {"opus": 1.0, "sonnet": 1.0, "haiku": 1.0}
    )
    states = {"m1": an.material_state("m1", runs)}
    gain = runs + [
        _run(1, md, run=r, p2=v)
        for md, v in {"opus": 1.0, "sonnet": 0.5, "haiku": 0.75}.items()
        for r in (1, 2)
    ]
    assert an.verdict_na({"m1": gain}, states)[0] == "통과"
    worse = runs + [
        _run(1, md, run=r, p2=v)
        for md, v in {"opus": 0.25, "sonnet": 1.0, "haiku": 1.0}.items()
        for r in (1, 2)
    ]
    assert an.verdict_na({"m1": worse}, states)[0] == "실패"
    flat = runs + [_run(1, md, run=r, p2=0.5) for md in ("opus", "sonnet", "haiku") for r in (1, 2)]
    assert an.verdict_na({"m1": flat}, states)[0] == "결론 없음"


def test_verdict_ba_compares_gaps_with_the_reference() -> None:
    runs = _base({"opus": 0.5, "sonnet": 0.5, "haiku": 0.5})
    states = {"m1": an.material_state("m1", runs)}  # 기준점 sonnet
    arm = [
        (1, "opus", 1.0, 1.0),
        (1, "sonnet", 0.5, 0.0),
        (3, "opus", 1.0, 1.0),
        (3, "sonnet", 1.0, 0.0),
    ]
    rows = runs + [_run(a, md, run=r, p2=p, hidden=h) for a, md, p, h in arm for r in (1, 2)]
    verdict, _, eff = an.verdict_ba({"m1": rows}, states)
    assert verdict == "통과" and eff == {"p2": 0.5, "hidden": 0.0}
    no_ref = {
        "m1": an.material_state(
            "m1",
            _base(
                {"opus": 0.5, "sonnet": 0.5, "haiku": 0.5},
                {"opus": 1.0, "sonnet": 0.5, "haiku": 0.5},
            ),
        )
    }
    assert an.verdict_ba({"m1": rows}, no_ref)[:2] == ("결론 없음", "기준점 없음")


def _q(arg: str, reply: Reply, t: str = T) -> Entry:
    return Entry(t=t, kind="question", arg=arg, reply=reply, text=reply.text)


def test_verdict_ma_pools_hit_rates() -> None:
    hit = Reply(text="a", rule=2, layer="answer", hit=1.0)
    miss = Reply(text="x", rule=5, layer="none", hit=0.0)
    p1 = _run(1, "opus", entries=[_q("a", hit), _q("b", hit)])
    p4 = _run(4, "opus", entries=[_q("a", hit), *(_q(str(i), miss) for i in range(4))])
    assert an.verdict_ma([p1, p4])[0] == "통과"  # 0.2 ≤ 1.0 × 0.5


def _material() -> Material:
    facts = [
        Fact(fact_id="f1", covers=["r1"], topic="회원별", match_terms=["회원당"], answer="한 번"),
        Fact(
            fact_id="f2", covers=["p2"], topic="수량", match_terms=["수량"], answer="정해 둔 만큼"
        ),
    ]
    return Material(
        mid="m1",
        alpha="data/eval/build/x",
        request="쿠폰 발급 기능을 붙여 주세요",
        facts=facts,
        req_ids={"p1": ["r1"], "p2": ["r2"]},
    )


def test_miss_causes_follow_the_cause_table() -> None:
    m = _material()
    opened = Reply(
        text="한 번",
        rule=2,
        layer="answer",
        item_id="f1",
        hit=1.0,
        candidates=["f1"],
        states={"f1": "open"},
    )
    broad = Reply(
        text=ONE_AT_A_TIME,
        rule=1,
        layer="one_at_a_time",
        hit=0.0,
        candidates=["f1", "f2"],
        states={},
    )
    late = _run(
        1,
        "opus",
        entries=[_q("회원당?", opened, "2026-11-13T10:05:00+09:00")],
        edits=["2026-11-13T01:00:00Z"],
    )
    assert an.miss_cause(m, late, "r1")[0] == "늦게 받음"  # 연 시각(01:05Z) 뒤 고친 흔적 없음
    wobble = late.model_copy(update={"edits": ["2026-11-13T01:10:00Z"]})
    assert an.miss_cause(m, wobble, "r1")[0] == "흔들림"
    blocked = _run(1, "opus", entries=[_q("회원당 수량?", broad)])
    assert an.miss_cause(m, blocked, "r2")[0] == "막힘"
    assert an.miss_cause(m, _run(1, "opus"), "r2")[0] == "묻지 않음"


def test_late_only_when_both_runs_show_it() -> None:
    m = _material()
    opened = Reply(
        text="한 번",
        rule=2,
        layer="answer",
        item_id="f1",
        hit=1.0,
        candidates=["f1"],
        states={"f1": "open"},
    )
    r1 = _run(1, "opus", run=1, made={"r1": False}, entries=[_q("회원당?", opened)])
    r2 = _run(1, "opus", run=2, made={"r1": True})
    rows = an.causes(m, [r1, r2])
    assert [x["cause"] for x in rows] == ["흔들림"]  # 한 번뿐이면 흔들림
    both = an.causes(
        m, [r1, r2.model_copy(update={"made": {"r1": False}, "entries": [_q("회원당?", opened)]})]
    )
    assert {x["cause"] for x in both} == {"늦게 받음"}
    assert an.next_cause_step(both) is not None and an.next_cause_step(both).startswith("늦게 받음")  # type: ignore[union-attr]


def test_limit_replay_finds_which_limit_cut_the_clue(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _material()
    task = SimpleNamespace(
        max_clarifications=2,
        max_requests=1,
        traps=[SimpleNamespace(trap_id="t1")],
        clarifications=[SimpleNamespace(clarification_id="c1", reveals=["t1"])],
        artifacts=[
            SimpleNamespace(artifact_id="a1", role="key"),
            SimpleNamespace(artifact_id="a2", role="distractor"),
        ],
    )
    monkeypatch.setattr(Material, "task", lambda self: task)
    miss = Reply(text="x", rule=5, layer="none", hit=0.0)
    clue = Reply(text="c", rule=2, layer="answer", item_id="c1", hit=1.0, states={"c1": "open"})
    fact = Reply(text="f", rule=2, layer="answer", item_id="f1", hit=1.0, states={"f1": "open"})
    late_clue = _run(
        1, "opus", entries=[_q("a", miss), _q("f", fact), _q("b", miss), _q("c", clue)]
    )
    assert an.limit_replay(m, late_clue) == "질문 쪽에서 잘림"  # f# 답은 빼고 세도 세 번째 질문
    early = _run(1, "opus", entries=[_q("f", fact), _q("c", clue)])
    assert an.limit_replay(m, early) == "남아 있음"
    served = [Entry(t=T, kind="material", served=x) for x in ("a2", "a1")]
    assert an.limit_replay(m, _run(1, "opus", entries=served)) == "자료 쪽에서 잘림"


def test_edit_times_and_forbidden_access() -> None:
    label = "m1-p1-opus-r1"
    events = [
        (
            "t1",
            "Edit",
            {"file_path": f"/ssd/solo_runs/analysis-pilot/ws/{label}/src/main/java/A.java"},
        ),
        ("t2", "Bash", {"command": "sed -i 's/a/b/' src/main/java/A.java"}),
        ("t3", "Read", {"file_path": "/ssd/make_task/CLAUDE.md"}),
        ("t4", "Bash", {"command": "cat /ssd/solo_runs/analysis-pilot/ws/m1-p2-opus-r1/README.md"}),
        (
            "t5",
            "Bash",
            {"command": f"cd /ssd/solo_runs/analysis-pilot/ws/{label} && ./gradlew test"},
        ),
    ]
    assert an.edit_times(events) == ["t1", "t2"]
    hits = an.forbidden_access(label, events)
    assert len(hits) == 2 and "make_task" in hits[0] and "m1-p2" in hits[1]


# ── #151 검토 4차: 유효성(돌연변이)·메서드 단위 요구·질문 시뮬레이션 ──


def _suite(fails: tuple[str, ...] = ()) -> SuiteRun:
    names = ("Pt1A.records", "Pt2B.keepsFailures", "Pt2B.newestFirst")
    return SuiteRun(
        setup="ok",
        methods=[
            Method(
                group="public",
                case="pt",
                cls=n.split(".")[0],
                method=n.split(".")[1],
                ok=n not in fails,
            )
            for n in names
        ],
    )


def _with_methods(m: Material) -> Material:
    f3 = Fact(fact_id="f3", covers=["r3"], topic="정렬", match_terms=["정렬"], answer="최신순")
    return m.model_copy(
        update={
            "facts": [*m.facts, f3],
            "method_req": {
                "Pt1A.records": ["r1"],
                "Pt2B.keepsFailures": ["r2"],
                "Pt2B.newestFirst": ["r3"],
            },
        }
    )


def test_validity_keeps_only_requirements_a_mutant_breaks() -> None:
    m = _with_methods(_material())
    meta = {
        "asc": {"req": "r3", "note": "오름차순"},
        "nofail": {"req": "r2", "note": "실패 미기록"},
    }
    runs = {
        "asc": _suite(("Pt2B.newestFirst",)),
        "nofail": _suite(),
    }  # 실패 미기록을 아무 메서드도 못 잡음
    out = judge(m, _suite(), runs, meta)
    assert out["baseline_ok"] and out["measured"] == ["r3"] and out["excluded"] == ["r1", "r2"]
    assert out["measured_unmentioned"] == ["r3"] and not out["usable"]  # 측정 요구 1개 < 2
    assert (
        judge(m, _suite(("Pt1A.records",)), runs, meta)["measured"] == []
    )  # 정답이 떨어지면 판정하지 않음


def test_validity_needs_every_mutant_of_a_requirement_caught() -> None:
    m = _with_methods(_material()).model_copy(update={"request_mentions": ["r2"]})
    meta = {
        "skip": {"req": "r1", "note": "빼먹기"},
        "asc": {"req": "r3", "note": "뒤집기"},
        "partial": {"req": "r3", "note": "지나침"},
        "nofail": {"req": "r2", "note": "빼먹기"},
    }
    runs = {
        "skip": _suite(("Pt1A.records",)),
        "asc": _suite(("Pt2B.newestFirst",)),
        "partial": _suite(),  # r3 의 돌연변이 하나가 빠져나감 → r3 는 측정 밖
        "nofail": _suite(("Pt2B.keepsFailures",)),
    }
    out = judge(m, _suite(), runs, meta)
    assert out["measured"] == ["r1", "r2"] and out["excluded"] == ["r3"]
    assert out["measured_unmentioned"] == ["r1"] and not out["usable"]  # r2 는 요청 글이 말함
    runs["partial"] = _suite(("Pt2B.newestFirst",))
    out = judge(m, _suite(), runs, meta)
    assert out["measured_unmentioned"] == ["r1", "r3"] and out["usable"]
    # 시작 코드가 이미 보여 주는 사실(정보 값 0)만 덮는 요구는 묻지 않고도 안다 → 세지 않는다
    zero = [f.model_copy(update={"info_zero": True}) if f.fact_id == "f1" else f for f in m.facts]
    out = judge(m.model_copy(update={"facts": zero}), _suite(), runs, meta)
    assert out["measured"] == ["r1", "r2", "r3"] and out["measured_unmentioned"] == ["r3"]
    assert not out["usable"]


def test_made_by_requirement_per_method(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _with_methods(_material())
    monkeypatch.setattr(Material, "measured", lambda self: ["r2", "r3"])
    made = an.made_by_requirement(m, _suite(("Pt2B.newestFirst",)))
    assert made == {"r2": True, "r3": False}  # r1 은 측정 밖, r2·r3 는 같은 클래스라도 따로


def test_simulation_classifies_questions() -> None:
    items = [F1, F2, C1]
    out = simulate_run(
        items,
        [
            {"q": "회원당 몇 번이요?", "about": "f1"},
            {"q": "점심은요?", "about": "f2"},
            {"q": "수량이랑 회원당은요?", "about": "f2"},
            {"q": "수량이랑 회원당은요?", "about": ["f1", "f2"]},
            {"q": "요청량은요?", "about": None},
        ],
    )
    assert [x["result"] for x in out["rows"]] == [
        "구체 답",
        "정보 없음",
        "한 가지씩",
        "여러 주제·한 가지씩",
        "주제 밖·잘못 열림",
    ]
    assert out["single_topic"] == 3  # 판정은 사실(f#) 한 항목 질문으로만
    assert out["rates"]["구체 답"] == 1 / 3 and out["rates"]["한 가지씩"] == 1 / 3
    assert out["off_topic_wrong_open"] == 1.0 and not out["passed"]
    assert classify(None, Reply(text=NO, rule=5, layer="none", hit=0.0)) == "주제 밖·정보 없음"


def test_simulation_passes_at_the_thresholds() -> None:
    qs = [{"q": "회원당 몇 번이요?", "about": "f1"}] * 8 + [
        {"q": "점심은요?", "about": "f2"},  # 정보 없음 1
        {"q": "수량이랑 회원당은요?", "about": "f2"},  # 한 가지씩 1
    ]
    out = simulate_run([F1, F2, C1], qs)
    assert out["rates"]["구체 답"] == 0.8 and out["rates"]["한 가지씩"] == 0.1 and out["passed"]
    out = simulate_run([F1, F2, C1], qs + [{"q": "수량이랑 회원당은요?", "about": "f1"}])
    assert not out["passed"]  # 구체 답 8/11 < 80%
    out = simulate_run([F1, F2, C1], qs + [{"q": "점심은요?", "about": "c1"}] * 5)
    assert (
        out["passed"] and out["clarification_rates"]["정보 없음"] == 1.0
    )  # 추가 요구(c#)는 기준 밖


def test_freeze_preflight_reads_only_the_official_judgments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("eval.pilot.model.DATA", tmp_path)
    m = _material()
    monkeypatch.setattr(freeze, "load_material", lambda mid: m)
    monkeypatch.setattr(freeze, "check_material", lambda mat: [])
    monkeypatch.setattr(freeze, "judged_version", lambda: 2)  # 진행자 프롬프트 지금 판
    qdir = m.dir / "questions"
    qdir.mkdir(parents=True)
    assert len(freeze.preflight("m1")) == 3  # 유효성 결과·공식 판정 둘 다 없음

    (m.dir / "validity.json").write_text(json.dumps({"baseline_ok": True, "usable": True}), "utf-8")
    record_official(
        qdir,
        {
            "kind": "general",
            "sets": ["check"],
            "version": 2,
            "stage": "result",
            "result": "check-llm-v2.result.json",
            "passed": False,
        },
    )
    record_official(
        qdir, {"kind": "vague", "sets": ["vague-check"], "version": 2, "stage": "declared"}
    )
    assert [p.split(":")[0] for p in freeze.preflight("m1")] == [
        "확인용 질문 목록이 기준에 못 미침 (질문 수가 모자라면 목록을 더 받아 합친다)",
        "모호한 질문 목록 공식 판정이 끝나지 않음",
    ]
    # 사실 질문이 모자라 목록을 더해 다시 — 마지막 공식 결과로 본다. 다른 판의 결과는 보지 않는다
    record_official(
        qdir,
        {
            "kind": "general",
            "sets": ["check", "check2"],
            "version": 2,
            "stage": "result",
            "result": "check+check2-llm-v2.result.json",
            "passed": True,
        },
    )
    record_official(
        qdir,
        {
            "kind": "vague",
            "sets": ["vague-check"],
            "version": 2,
            "stage": "result",
            "result": "vague-check-llm-v2.result.json",
            "passed": True,
        },
    )
    record_official(
        qdir,
        {
            "kind": "general",
            "sets": ["x"],
            "version": 1,
            "stage": "result",
            "result": "x",
            "passed": False,
        },
    )
    assert freeze.preflight("m1") == []


def test_simulation_needs_enough_questions_and_pools_lists(tmp_path: Path) -> None:
    few = simulate_run([F1, F2, C1], [{"q": "회원당 몇 번이요?", "about": "f1"}] * 7)
    assert (
        few["rates"]["구체 답"] == 1.0 and not few["facts_enough"] and not few["passed"]
    )  # 사실 질문 7 < 8
    enough = simulate_run([F1, F2, C1], [{"q": "회원당 몇 번이요?", "about": "f1"}] * 8)
    assert enough["facts_enough"] and enough["passed"]
    vague = simulate_run([F1, F2, C1], [{"q": "주의할 점이 있나요?", "about": "vague"}] * 19)
    assert vague["vague_open_rate"] == 0.0 and not vague["vague_passed"]  # 모호 질문 19 < 20
    one_open = [{"q": "회원당 몇 번이요?", "about": "vague"}] + [
        {"q": "주의할 점이 있나요?", "about": "vague"}
    ] * 19
    r = simulate_run([F1, F2, C1], one_open)
    assert r["vague_open_rate"] == 0.05 and r["vague_passed"]  # 20개 중 1개까지
    (tmp_path / "a.json").write_text(json.dumps([{"q": "1", "about": None}]), "utf-8")
    (tmp_path / "b.json").write_text(json.dumps([{"q": "2", "about": "f1"}]), "utf-8")
    assert [q["q"] for q in pooled(tmp_path, ["a", "b"])] == ["1", "2"]  # 만든 순서대로


def test_session_drops_request_words_from_topic_hints() -> None:
    q = "이벤트 언제 해요?"
    assert _ask(_session(), [], "question", q) == "설정에 따라요."  # f2 주제 '이벤트' 가 겹침
    s = _session(
        request="이벤트 쿠폰을 발급해 주세요"
    )  # 요청 글에 있는 말은 주제 단서가 아님 (§4.6 (ii))
    assert _ask(s, [], "question", q) == NO


# ── 진행자 LLM (응시 플랫폼 흉내, eval/pilot/llm_facilitator.py) — 가짜 LLM ──


def _picked(*pairs: tuple[str, bool]) -> list[AskedItem]:
    return [AskedItem(item_id=k, concrete=c) for k, c in pairs]


def test_llm_choice_follows_the_answer_rule_states() -> None:
    items = [F1, F2, C1]  # F1 모호한 답 없음, F2·C1 있음
    r = apply_choice(_picked(("f1", True)), items, {}, legacy=False)
    assert (r.rule, r.item_id, r.layer, r.hit, r.states["f1"]) == (2, "f1", "answer", 1.0, "open")
    both = _picked(("f1", True), ("f2", True))
    assert apply_choice(both, items, {}, legacy=False).text == ONE_AT_A_TIME
    r = apply_choice(both, items, {"f1": "open"}, legacy=False)  # 열린 것은 세지 않는다 — 이어 묻기
    assert (r.rule, r.item_id) == (2, "f2")
    again = apply_choice(_picked(("f1", True)), items, {"f1": "open"}, legacy=False)
    assert (again.rule, again.repeat, again.hit) == (3, True, None)
    assert apply_choice(_picked(("f2", False)), items, {}, legacy=False).text == "설정에 따라요."
    assert apply_choice(_picked(("f1", False)), items, {}, legacy=False).text == NO  # §4.6 (iv)
    assert apply_choice([], items, {}, legacy=False).text == NO
    assert apply_choice(_picked(("f9", True)), items, {}, legacy=False).text == NO  # 모르는 id
    legacy = apply_choice(_picked(("c1", False), ("f2", True)), [C1], {}, legacy=True)
    assert (legacy.rule, legacy.item_id, legacy.layer, legacy.hit) == (2, "c1", "hint", None)


def test_render_items_shows_condition_sentence_or_current_terms() -> None:
    fact = F1.model_copy(update={"ask_when": "회원 한 명이 몇 번 받을 수 있는지 물을 때"})
    new = render_items([fact, C1], legacy=False)
    assert (
        "구체 조건(ask_when): 회원 한 명이" in new
        and "매칭 낱말(match_terms, 예시 표현): 요청량" in new
    )
    old = render_items([C1], legacy=True)
    assert "매칭 낱말(match_terms): 요청량" in old and "ask_when" not in old


class _FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def call(self, prompt: Any, out: Any) -> Any:
        self.calls += 1
        parsed = FacilitatorChooseOutput(asked=_picked(("f1", True), ("zz", True)))
        return SimpleNamespace(
            parsed=parsed,
            provenance=SimpleNamespace(model_dump=lambda mode="json": {"model": "fake"}),
        )


def test_chooser_caches_answers_and_drops_unknown_ids(tmp_path: Path) -> None:
    fake = _FakeLLM()
    cache = tmp_path / "cache.json"
    c = Chooser(llm=fake, cache=cache)  # type: ignore[arg-type]
    first = c.choose_sync("회원당 몇 번?", [F1, F2], False, "요청 글")
    second = c.choose_sync("회원당   몇 번?", [F1, F2], False, "요청 글")  # 공백만 다름 — 같은 질문
    assert [x.item_id for x in first] == ["f1"] and first == second and fake.calls == 1
    again = Chooser(llm=fake, cache=cache)  # type: ignore[arg-type]
    assert (
        again.choose_sync("회원당 몇 번?", [F1, F2], False, "요청 글") == first and fake.calls == 1
    )
    assert json.loads(cache.read_text("utf-8"))  # provenance 와 함께 남음


def test_simulation_can_use_the_llm_chooser() -> None:
    out = simulate_run(
        [F1, F2, C1],
        [{"q": "한 사람이 여러 번 받나요?", "about": "f1"}, {"q": "배포는요?", "about": None}],
        "",
        choose=lambda q: _picked(("f1", True)) if "사람" in q else [],
    )
    assert [x["result"] for x in out["rows"]] == ["구체 답", "주제 밖·정보 없음"]
