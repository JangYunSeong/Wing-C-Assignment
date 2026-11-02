from __future__ import annotations

import json
from pathlib import Path

import pytest

from task_spec.llm import Completion, FakeBackend, LLMClient
from task_spec.postings.sections import segment, segment_rules
from task_spec.prompting import PromptRegistry
from tests.postings.fixtures import NO_HEADINGS, POSTING

PROMPTS = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")


def test_rules_sections_bullets_and_group_titles() -> None:
    lines = segment_rules(POSTING)
    by = {
        s: [x.text for x in lines if x.section == s]
        for s in ("duties", "required", "preferred", "other")
    }
    assert by["duties"] == [
        "대용량 로그 데이터 파이프라인 설계 및 구축",
        "Airflow 기반 배치 워크플로우 운영 및 자동화",
        "Spark 를 활용한 데이터 처리 성능 최적화",
    ]  # 묶음 제목(< 데이터 파이프라인 >) 제외, 글머리 기호 제거
    assert by["required"][0] == "Python, SQL 을 활용한 데이터 처리 경험"  # "1." 제거
    assert len(by["preferred"]) == 2
    # 회사 소개·복지 절 → other (분석하지 않는 절). 첫 제목 전 줄(공고 제목)은 버린다
    assert by["other"] == ["가상테크는 가상의 커머스 데이터 회사입니다.", "자율 출퇴근"]
    assert not any("채용 (가상 데이터)" in x.text for x in lines)


@pytest.mark.parametrize(
    ("heading", "section"),
    [
        ("주요 업무", "duties"),
        ("이런 일을 해요", "duties"),
        ("필수 요건", "required"),
        ("이런 분이면 더 좋아요", "preferred"),
        ("■ 우대 ■", "preferred"),
        ("Responsibilities", "duties"),
    ],
)
def test_heading_variants(heading: str, section: str) -> None:
    lines = segment_rules(f"{heading}\n- 무언가를 한다")
    assert lines and lines[0].section == section


def test_sentence_starting_with_heading_word_is_not_heading() -> None:
    lines = segment_rules("담당업무\n- 우대 고객 응대 시스템을 개발한다")
    assert lines[0].section == "duties"


async def test_llm_fallback_only_without_headings_and_drops_fabrications() -> None:
    body = {
        "lines": [
            {"text": "대용량 로그 데이터 파이프라인을 설계하고 구축합니다.", "section": "duties"},
            {"text": "Python 과 SQL 에 능숙한 분이면 좋습니다.", "section": "required"},
            {"text": "원문에 없는 지어낸 자격요건", "section": "required"},
        ]
    }
    fake = FakeBackend().push(Completion(json.dumps(body, ensure_ascii=False)))
    seg = await segment(NO_HEADINGS, LLMClient({"openai": fake}), PROMPTS)
    assert seg.method == "llm" and [x.text for x in seg.lines] == [
        b["text"] for b in body["lines"][:2]
    ]
    seg2 = await segment(
        POSTING, LLMClient({"openai": FakeBackend()}), PROMPTS
    )  # 제목 있으면 LLM 호출 없음
    assert seg2.method == "rules"


async def test_no_headings_without_llm() -> None:
    seg = await segment(NO_HEADINGS, None, None)
    assert seg.method == "none" and seg.lines == []
