"""손 시범 도구 (eval/pilot, docs/design/analysis-stage.md §9.1) — 순수한 부분만. 가상 데이터."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eval.pilot import analyze as an
from eval.pilot.analyze import RunData
from eval.pilot.facilitator import (
    EMPTY_QUESTION,
    Entry,
    Session,
    client_script,
    handle,
    states,
)
from eval.pilot.grade import methods
from eval.pilot.model import Fact, Material, parse_label, run_label, term_overlaps
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
<testcase classname="com.example.service.publictests.Pt1ListTest" name="b"><failure message="expected 409"/></testcase>
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
