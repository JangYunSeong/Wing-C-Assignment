"""진행자 답 규칙 (docs/design/analysis-stage.md §4.6, §9.1 판 3) — 가상 데이터 (이벤트 쿠폰 예시)."""

from __future__ import annotations

from task_spec.starter.answer_rule import (
    NO_INFO,
    ONE_AT_A_TIME,
    Reply,
    State,
    answer,
    answer_legacy,
    hit_rate,
    item_order,
    topic_words,
)
from task_spec.starter.facilitator import Answer

F1 = Answer(
    id="f1",
    topic="회원별 발급 제한",
    match_terms=["회원당", "여러 번", "중복 발급"],
    answer="회원당 한 번이에요.",
    answer_hint=None,
)
F2 = Answer(
    id="f2",
    topic="이벤트 쿠폰 수량",
    match_terms=["수량", "몇 장"],
    answer="이벤트마다 정해 둔 수량만큼이에요.",
    answer_hint="이벤트 설정에 따라 달라요.",
)
C1 = Answer(
    id="c1",
    topic="이벤트 오픈 직후 접속량",
    match_terms=["요청량", "트래픽"],
    answer="오픈 직후 초당 요청이 평소의 수십 배예요.",
    answer_hint="'운영 지표' 자료에서 확인할 수 있습니다.",
)
C2 = Answer(
    id="c2",
    topic="앱 재시도 동작",
    match_terms=["재시도", "retry"],
    answer="응답이 늦으면 앱이 자동으로 다시 보내요.",
    answer_hint=None,
)
ITEMS = [F1, F2, C1, C2]
NONE: dict[str, State] = {}


def test_item_order_puts_facts_first_then_numbers() -> None:
    assert sorted(["c1", "f10", "f2", "f1"], key=item_order) == ["f1", "f2", "f10", "c1"]


def test_topic_words_drop_general_words() -> None:
    assert topic_words("이벤트 쿠폰 수량") == ["이벤트"]  # 두 글자 이하는 일반어
    assert topic_words("이벤트 오픈 직후 접속량") == ["이벤트", "접속량"]


def test_two_unopened_items_get_one_at_a_time() -> None:
    r = answer("수량이랑 재시도는 어떻게 되나요?", ITEMS, NONE)
    assert (r.rule, r.text, r.layer, r.hit) == (1, ONE_AT_A_TIME, "one_at_a_time", 0.0)
    assert r.candidates == ["f2", "c2"]
    assert set(r.states.values()) == {"none"}  # 아무것도 열리지 않는다


def test_one_unopened_item_gets_the_concrete_answer_and_opens() -> None:
    r = answer("몇 장까지 받을 수 있나요?", ITEMS, NONE)
    assert (r.rule, r.item_id, r.text, r.hit) == (2, "f2", F2.answer, 1.0)
    assert r.states["f2"] == "open"


def test_terms_ignore_spacing_and_particles_like_match_terms_today() -> None:
    r = answer("회원 당 여러번 받을 수 있나요?", ITEMS, NONE)  # 띄어쓰기·조사가 달라도 걸린다
    assert (r.rule, r.item_id) == (2, "f1")
    assert answer("RETRY 정책이 있나요?", ITEMS, NONE).item_id == "c2"  # 영문은 대소문자 무시
    assert answer("retrying 하나요?", ITEMS, NONE).rule != 2  # 영문은 단어 경계


def test_follow_up_counts_only_unopened_items() -> None:
    """'수량 말고 재시도는요?' — 열린 f2 는 세지 않아 c2 하나만 걸린다 (규칙 2)."""
    opened: dict[str, State] = {"f2": "open"}
    r = answer("수량 말고 재시도는요?", ITEMS, opened)
    assert (r.rule, r.item_id, r.text, r.hit) == (2, "c2", C2.answer, 1.0)
    assert r.states == {"f1": "none", "f2": "open", "c1": "none", "c2": "open"}


def test_only_opened_items_repeat_the_same_answer_without_credit() -> None:
    r = answer("수량은요?", ITEMS, {"f2": "open"})
    assert (r.rule, r.item_id, r.text, r.repeat, r.hit) == (3, "f2", F2.answer, True, None)
    both: dict[str, State] = {"f1": "open", "f2": "open"}
    r2 = answer("회원당 몇 장 수량", ITEMS, both)  # f2 가 두 낱말로 더 많이 맞음
    assert (r2.rule, r2.item_id) == (3, "f2")


def test_topic_overlap_gives_the_hint_with_half_credit_when_unique() -> None:
    r = answer("접속량이 많나요?", ITEMS, NONE)
    assert (r.rule, r.item_id, r.text, r.layer, r.hit) == (4, "c1", C1.answer_hint, "hint", 0.5)
    assert r.states["c1"] == "hint"


def test_topic_overlap_on_two_items_gives_no_credit() -> None:
    r = answer("이벤트 때 접속량은 어떤가요?", ITEMS, NONE)  # c1 은 둘(이벤트·접속량), f2 는 하나
    assert (r.rule, r.item_id, r.hit) == (4, "c1", 0.0)
    assert r.candidates == ["f2", "c1"]
    tie = answer("이벤트 운영 방식이 궁금해요", ITEMS, NONE)  # 같으면 id 순, f 먼저
    assert (tie.item_id, tie.text, tie.hit) == ("f2", F2.answer_hint, 0.0)


def test_topic_overlap_without_hint_gives_the_answer_and_opens() -> None:
    r = answer("회원별로 다른 게 있나요?", ITEMS, NONE)
    assert (r.rule, r.item_id, r.text, r.layer, r.hit) == (4, "f1", F1.answer, "answer", 0.5)
    assert r.states["f1"] == "open"


def test_topic_overlap_skips_opened_items_and_repeats_hints() -> None:
    assert answer("회원별로 다른 게 있나요?", ITEMS, {"f1": "open"}).text == NO_INFO
    r = answer("접속량이 많나요?", ITEMS, {"c1": "hint"})
    assert (r.rule, r.text, r.repeat, r.hit) == (4, C1.answer_hint, True, None)
    assert r.states["c1"] == "hint"
    exact = answer(
        "트래픽이 얼마나 되나요?", ITEMS, {"c1": "hint"}
    )  # 모호 뒤 정확히 짚으면 구체 답
    assert (exact.rule, exact.text, exact.states["c1"]) == (2, C1.answer, "open")


def test_nothing_matches_gives_no_information() -> None:
    r = answer("점심 메뉴는 뭔가요?", ITEMS, NONE)
    assert (r.rule, r.text, r.layer, r.hit) == (5, NO_INFO, "none", 0.0)


def test_same_question_same_reply() -> None:
    q = "이벤트 때 접속량은 어떤가요?"
    assert answer(q, ITEMS, NONE) == answer(q, ITEMS, NONE)


def test_legacy_mode_answers_the_best_match_without_one_at_a_time() -> None:
    clar = [C1, C2]
    r = answer_legacy("재시도랑 요청량은요?", clar)  # 둘 다 한 낱말 — id 순
    assert (r.rule, r.item_id, r.text, r.hit, r.states) == (1, "c1", C1.answer, None, {})
    assert r.candidates == ["c1", "c2"]
    hint = answer_legacy("접속량이 많나요?", clar)
    assert (hint.rule, hint.text, hint.layer) == (2, C1.answer_hint, "hint")
    assert answer_legacy("접속량이 많나요?", clar) == hint  # 다시 물어도 같은 답
    assert answer_legacy("점심 메뉴는요?", clar).rule == 3


def test_hit_rate_leaves_repeats_out() -> None:
    replies = [
        Reply(text="a", rule=2, layer="answer", hit=1.0),
        Reply(text="h", rule=4, layer="hint", hit=0.5),
        Reply(text="a", rule=3, layer="answer", repeat=True),
        Reply(text=NO_INFO, rule=5, layer="none", hit=0.0),
    ]
    assert hit_rate(replies) == 0.5
    assert hit_rate([]) is None
