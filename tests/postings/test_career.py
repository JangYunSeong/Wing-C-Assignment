"""공고 신입·경력 (postings/career.py) — 문구 추정 규칙과 정한 값 우선 (API 는 tests/api/test_postings_career). 가상 데이터만.

추정 문장은 2026-11-19 온톨로지 공고 자격요건에 실제로 나온 꼴을 본떴다 ('경력 2년 이상'·'N년차'·'[신입]'·'신입 또는 주니어급').
"""

from __future__ import annotations

import pytest

from task_spec.postings.career import (
    PostingCareer,
    estimate_career,
    posting_careers,
    provisional_career_texts,
)
from tests.api.conftest import FakeOntology, FakeProvisional


@pytest.mark.parametrize(
    ("texts", "want"),
    [
        (["가상회사 / 가상 직무 (경력)"], "경력"),
        (["가상 직무", "경력 2년 이상"], "경력"),
        (["가상 직무", "가상 분야 실무 5년 이상"], "경력"),
        (["가상 직무", "유관 경력 7년차~12년차"], "경력"),
        (["가상 직무", "[신입]"], "신입"),
        (["가상 직무", "신입 또는 주니어급으로, 가상 분야에 관심이 있는 분"], "신입"),
        (["가상 직무", "신입/경력 3년 이상"], None),  # 둘 다
        (["가상 직무", "경력 무관"], None),
        (["가상 직무", "가상 도구 사용 경험"], None),  # 단서 없음
        ([], None),
    ],
)
def test_estimate_career(texts: list[str], want: str | None) -> None:
    assert estimate_career(texts) == want


def test_provisional_texts_are_title_and_required_lines_only() -> None:
    """담당업무('신입 개발자 채용 운영')·우대('경력자 우대')는 공고의 신입·경력을 말하지 않는다."""
    row = {
        "title": "가상 직무",
        "lines": [
            {"section": "duties", "text": "신입 개발자 채용 운영"},
            {"section": "preferred", "text": "가상 분야 경력 3년 이상 우대"},
            {"section": "required", "text": "가상 도구 사용 경험"},
        ],
    }
    assert provisional_career_texts(row) == ["가상 직무", "가상 도구 사용 경험"]
    assert estimate_career(provisional_career_texts(row)) is None


async def test_confirmed_value_wins_over_posting_text() -> None:
    onto = FakeOntology(
        {},
        [],
        careers={"가상_공고#001": ["가상 직무", "경력 3년 이상"], "가상_공고#002": ["가상 직무"]},
    )
    store = FakeProvisional(
        {"prov-fake0001": None}, rows={"prov-fake0001": {"title": "[신입] 가상 직무"}}
    )
    store.levels["가상_공고#001"] = "신입"
    got = await posting_careers(
        ["가상_공고#001", "가상_공고#002", "prov-fake0001", "없는_공고"],
        ontology=onto,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
    )
    assert got == {
        "가상_공고#001": PostingCareer("신입", "confirmed"),
        "prov-fake0001": PostingCareer("신입", "posting_text"),
    }
