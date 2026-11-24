"""수리 루프의 '계약 탓' 판정 (#256 4단계) — 같은 꼴이 테스트를 다시 만든 뒤에도 되풀이되면 계약 결함 의심으로 멈추고,
실패한 빌드에는 어느 관문에서 멈췄든 마지막 검증의 계약 탓 꼴을 남긴다 (starter/repair.py · build.build_and_repair)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from task_spec.nodes.starter import build as build_mod
from task_spec.nodes.starter.build import MAX_SIGNALS, SIGNAL_FLAG, BuildOutcome, build_and_repair
from task_spec.schemas import VerifyReport
from task_spec.starter.repair import (
    CONTRACT_SUSPECT,
    RegenNote,
    assign,
    contract_signals,
)
from task_spec.starter.static_check import Overlay
from tests.starter.test_generate import D1, D2, _inputs
from tests.starter.test_hand_gate import _refs
from tests.starter.test_repair import PROMPTS, _one_case, _run, _t

PUB = "OrderHistoryPublicTest"
REASON = "pool_acquire_timeout"


def _plan() -> Any:
    return _inputs().plan


def _case() -> str:
    return _plan().cases[0].case_id


def _flip() -> VerifyReport:
    """흔한 구현은 통과, 허용 설계 둘은 같은 단언으로 실패 — 11-18 뒤집힘 꼴."""
    c = _case()
    return _one_case(
        _run("naive-all", "ref_naive", _t("hidden", c), _t("public", PUB)),
        _run(
            "d1",
            "ref_accepted",
            _t("hidden", c, "ASSERT", REASON, "3 건 시간 초과"),
            _t("public", PUB),
        ),
        _run(
            "d2",
            "ref_accepted",
            _t("hidden", c, "ASSERT", REASON, "3 건 시간 초과"),
            _t("public", PUB),
        ),
    )


def _all_public_fail(msg_d2: str = "기대 200 실제 500") -> VerifyReport:
    c = _case()
    return _one_case(
        _run(
            "naive-all",
            "ref_naive",
            _t("hidden", c, "ASSERT", REASON),
            _t("public", PUB, "ASSERT", None, "기대 200 실제 500"),
        ),
        _run(
            "d1",
            "ref_accepted",
            _t("hidden", c),
            _t("public", PUB, "ASSERT", None, "기대 200 실제 500"),
        ),
        _run("d2", "ref_accepted", _t("hidden", c), _t("public", PUB, "ASSERT", None, msg_d2)),
    )


def _assign(
    report: VerifyReport,
    counts: dict[str, int] | None = None,
    log: dict[str, RegenNote] | None = None,
) -> Any:
    b = _inputs()
    return assign(
        report,
        b.plan,
        {"starter": Overlay(kind="starter", files=())},
        {"naive-all": {t.trap_id for t in b.task.traps}},
        counts or {},
        regen_log=log or {},
    )


def test_signals_name_the_flip_and_the_test_every_implementation_fails_the_same_way() -> None:
    c = _case()
    sig = contract_signals(_flip(), _plan())
    assert [(s.kind, s.group, s.name) for s in sig] == [("flip", "hidden", c)]
    assert (
        "흔한 구현만 통과, 허용 설계 모두 실패" in sig[0].line()
        and "3 건 시간 초과" in sig[0].line()
    )

    sig = contract_signals(_all_public_fail(), _plan())
    assert [(s.kind, s.group, s.name) for s in sig] == [("all_same", "public", PUB)]
    assert "모든 구현이 같은 이유로 실패" in sig[0].line()
    # 서명이 다르면 같은 이유가 아니다 (숫자만 다른 것은 같은 서명)
    assert contract_signals(_all_public_fail("NullPointerException at Foo"), _plan()) == []
    assert contract_signals(_all_public_fail("기대 200 실제 503"), _plan()) != []


def test_signals_ignore_implementations_that_did_not_start() -> None:
    c = _case()
    report = _one_case(
        _run("naive-all", "ref_naive", _t("hidden", c), _t("public", PUB)),
        _run("d1", "ref_accepted", _t("hidden", c, "ASSERT", REASON, "실패"), _t("public", PUB)),
        _run("d2", "ref_accepted", setup="compile_error"),
    )
    assert [s.kind for s in contract_signals(report, _plan())] == ["flip"]


def test_a_flip_tells_the_hidden_test_generator_that_only_the_naive_passes() -> None:
    plan = _assign(_flip())
    hidden = [a for a in plan.actions if a.generator == "gen_hidden" and "④" in a.gate]
    assert hidden, plan.actions
    assert "흔한 구현만 통과" in hidden[0].reason
    assert "흔한 구현은 이 케이스를 통과한다" in hidden[0].note
    assert _case() in hidden[0].cases


def test_the_same_all_fail_after_the_test_was_rebuilt_is_a_contract_suspect() -> None:
    first = _assign(_flip())
    a = next(x for x in first.actions if x.generator == "gen_hidden")
    again = _assign(
        _flip(), {"gen_hidden": 1}, {"gen_hidden": RegenNote(a.gate, a.cases, a.reason, a.blame)}
    )
    assert not [x for x in again.actions if x.generator == "gen_hidden"]
    suspect = [u for u in again.unresolved if u.startswith(CONTRACT_SUSPECT)]
    assert (
        len(suspect) == 1 and f"hidden:{_case()}" in suspect[0] and "다시 만든 뒤에도" in suspect[0]
    )

    # 다른 이유로 숨은 테스트를 다시 만들었으면 '되풀이'가 아니다 — 예전처럼 수리로 남는다 (예산 소진으로 멈춤)
    other = _assign(
        _flip(), {"gen_hidden": 1}, {"gen_hidden": RegenNote("③", ("hc9",), "흔한 구현이 hc9 통과")}
    )
    assert [x for x in other.actions if x.generator == "gen_hidden"]
    assert not [u for u in other.unresolved if u.startswith(CONTRACT_SUSPECT)]


def test_a_public_test_every_implementation_fails_twice_is_a_contract_suspect() -> None:
    first = _assign(_all_public_fail())
    a = next(x for x in first.actions if x.generator == "gen_public")
    assert a.reason.startswith("모든 구현이 공개 테스트")
    again = _assign(
        _all_public_fail(),
        {"gen_public": 1},
        {"gen_public": RegenNote(a.gate, a.cases, a.reason, a.blame)},
    )
    assert not [x for x in again.actions if x.generator == "gen_public"]
    assert any(u.startswith(f"{CONTRACT_SUSPECT} — public:{PUB}") for u in again.unresolved)


class _HandOnly:
    """손 허용 구현만 도는 변형 실행 — 결과를 정해 준다."""

    def __init__(self, result: str) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    async def verify(self, *a: Any, **k: Any) -> VerifyReport:  # 쓰이지 않음
        raise AssertionError

    async def verify_variants(
        self,
        starter: Overlay,
        variants: Sequence[Overlay],
        public: Overlay,
        hidden: Overlay | None = None,
    ) -> VerifyReport:
        self.calls.append([v.label for v in variants])
        return VerifyReport(
            runs=[
                _run(v.label, "ref_accepted", _t("hidden", _case(), self.result)) for v in variants
            ]
        )


@pytest.mark.parametrize(
    ("hand", "evidence"),
    [
        ("PASS", "손 허용 구현 2개 통과 — 테스트는 올바른 구현으로 통과 가능, 생성 구현 결함 의심"),
        ("ASSERT", "손 허용 구현 2개도 실패 — 테스트·계약 결함 의심"),
    ],
)
async def test_a_failed_build_reports_the_signals_with_hand_evidence(
    monkeypatch: pytest.MonkeyPatch, hand: str, evidence: str
) -> None:
    empty = Overlay(kind="starter", files=())
    failed = BuildOutcome(
        status="failed",
        reports=[_flip()],
        overlays={
            "starter": empty,
            "hidden": Overlay(kind="hidden", files=()),
            "public": Overlay(kind="public_test", files=()),
        },
        review_flags=["재생성 예산을 소진함 — gen_public: 다른 관문 이유"],
    )

    async def fake_loop(*a: Any, **k: Any) -> BuildOutcome:
        return failed

    monkeypatch.setattr(build_mod, "_build_and_repair", fake_loop)
    refs = _refs(("acc1", "accepted", []), ("acc2", "accepted", []), ("naive", "naive", [REASON]))
    verifier = _HandOnly(hand)
    out = await build_and_repair(
        _inputs(references=refs),
        (D1, D2),
        llm=None,
        prompts=PROMPTS,
        ontology_version="t",
        verifier=verifier,  # type: ignore[arg-type]
    )
    assert verifier.calls == [["hand-acc1", "hand-acc2"]]  # 허용 손 구현만
    assert len(out.blame_reports) == 1 and out.hand_reports == []  # 손 관문 보고서와 따로
    signal = [f for f in out.review_flags if f.startswith(SIGNAL_FLAG)]
    assert len(signal) == 1 and "흔한 구현만 통과" in signal[0] and evidence in signal[0]
    assert out.review_flags[0].startswith("재생성 예산을 소진함")  # 멈춘 관문의 표시는 그대로


async def test_signals_are_left_without_hand_evidence_and_never_on_a_passed_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = BuildOutcome(status="failed", reports=[_flip()])

    async def fake_loop(*a: Any, **k: Any) -> BuildOutcome:
        return outcome

    monkeypatch.setattr(build_mod, "_build_and_repair", fake_loop)
    out = await build_and_repair(
        _inputs(),
        (D1, D2),
        llm=None,
        prompts=PROMPTS,
        ontology_version="t",
        verifier=_HandOnly("PASS"),  # type: ignore[arg-type]
    )
    assert [
        f for f in out.review_flags if f.startswith(SIGNAL_FLAG)
    ] and "손 허용" not in out.review_flags[-1]

    outcome = BuildOutcome(status="review", reports=[_flip()])
    out = await build_and_repair(
        _inputs(),
        (D1, D2),
        llm=None,
        prompts=PROMPTS,
        ontology_version="t",
        verifier=_HandOnly("PASS"),  # type: ignore[arg-type]
    )
    assert out.review_flags == []


def test_recurrence_is_read_from_the_test_id_not_the_reason_text() -> None:
    """리뷰 2026-11-24: 사유는 `_merge` 가 합치고 220자로 자르며, 뒤집힘 꼬리말이 회차마다 바뀐다 — 테스트 id 칸(blame)으로 본다."""
    c = _case()
    # 첫 회차는 흔한 구현도 떨어졌고(뒤집힘 아님) 둘째 회차는 흔한 구현만 통과 — 같은 케이스의 되풀이
    first = _one_case(
        _run(
            "naive-all",
            "ref_naive",
            _t("hidden", c, "ASSERT", "other_reason", "다른 단언"),
            _t("public", PUB),
        ),
        _run(
            "d1",
            "ref_accepted",
            _t("hidden", c, "ASSERT", REASON, "3 건 시간 초과"),
            _t("public", PUB),
        ),
        _run(
            "d2",
            "ref_accepted",
            _t("hidden", c, "ASSERT", REASON, "3 건 시간 초과"),
            _t("public", PUB),
        ),
    )
    a = next(x for x in _assign(first).actions if x.generator == "gen_hidden")
    assert "흔한 구현만 통과" not in a.reason and a.blame == (f"hidden:{c}",)
    again = _assign(
        _flip(), {"gen_hidden": 1}, {"gen_hidden": RegenNote(a.gate, a.cases, a.reason, a.blame)}
    )
    assert any(u.startswith(CONTRACT_SUSPECT) for u in again.unresolved)
    # 앞 사유가 길어 사유 글이 잘려도 id 로 본다
    long_reason = "가" * 400 + "; 허용 구현 모두"
    note = RegenNote("③④", (c,), long_reason[:220], (f"hidden:{c}",))
    again = _assign(_flip(), {"gen_hidden": 1}, {"gen_hidden": note})
    assert any(u.startswith(f"{CONTRACT_SUSPECT} — hidden:{c}") for u in again.unresolved)


async def test_signal_flags_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    names = [f"Pub{i}Test" for i in range(MAX_SIGNALS + 2)]
    runs = [
        _run(t, k, *[_t("public", n, "ASSERT", None, "기대 200 실제 500") for n in names])
        for t, k in (("naive-all", "ref_naive"), ("d1", "ref_accepted"), ("d2", "ref_accepted"))
    ]
    outcome = BuildOutcome(status="failed", reports=[_one_case(*runs)])

    async def fake_loop(*a: Any, **k: Any) -> BuildOutcome:
        return outcome

    monkeypatch.setattr(build_mod, "_build_and_repair", fake_loop)
    out = await build_and_repair(
        _inputs(),
        (D1, D2),
        llm=None,
        prompts=PROMPTS,
        ontology_version="t",
        verifier=_HandOnly("PASS"),  # type: ignore[arg-type]
    )
    shown = [f for f in out.review_flags if f.startswith(SIGNAL_FLAG)]
    assert (
        len(shown) == MAX_SIGNALS + 1
        and shown[-1] == f"{SIGNAL_FLAG}외 2건 (보고서의 마지막 검증 참고)"
    )
