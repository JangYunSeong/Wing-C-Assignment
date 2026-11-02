from __future__ import annotations

import pytest

from task_spec.nodes.ingest.structure import structure


def test_fields_headers_sections_and_markers() -> None:
    text, sections = structure(
        [
            "경력",
            "가상데이터랩 데이터 엔지니어  2023.01 ~ 2024.12",
            "문제 정의: 집계가 느렸다.",
            "과정: DAG 를 재구성했다.",
            "  이어지는 과정 설명 줄",
            "기여한 점: 6시간에서 40분으로 줄였다.",
            "",
            "■ 학력 ■",
            "가상대학교 2016.03 ~ 2020.02",
        ]
    )
    assert "⟦문제 정의⟧ 집계가 느렸다." in text
    assert "⟦과정⟧ DAG 를 재구성했다." in text
    assert "⟦기여한 점⟧ 6시간에서 40분으로 줄였다." in text
    kinds = [s.kind for s in sections]
    assert kinds == ["경력", "학력"]
    career = sections[0]
    assert career.headers[0].period == "2023.01 ~ 2024.12"
    assert [f.label for f in career.fields] == ["문제 정의", "과정", "기여한 점"]
    assert career.fields[1].text == "DAG 를 재구성했다.\n이어지는 과정 설명 줄"


@pytest.mark.parametrize(
    ("line", "label"),
    [
        ("[문제정의] 느렸다", "문제 정의"),
        ("【과정】 재구성", "과정"),
        ("- 기여도: 40%", "기여한 점"),
        ("▶ 담당 업무 : 설계", "담당업무"),
        ("역할 - 리드", "담당업무"),
        ("결과: 단축", "성과"),
        ("기술 스택: Spark", "사용 기술"),
        ("과정", "과정"),  # 라벨만 있는 줄
    ],
)
def test_label_variants(line: str, label: str) -> None:
    _, sections = structure([line])
    assert sections[0].fields[0].label == label


@pytest.mark.parametrize(
    "line",
    [
        "과정에서 많은 것을 배웠다.",
        "결과적으로 비용이 줄었다.",
        "문제를 정의하는 것이 중요했다.",
        "담당자와 협의했다.",
    ],
)
def test_sentences_starting_with_label_words_are_not_fields(line: str) -> None:
    text, sections = structure([line])
    assert "⟦" not in text
    assert all(not s.fields for s in sections)


@pytest.mark.parametrize(
    "line",
    [
        "2021.03 – 현재  가상회사",
        "2019년 3월 ~ 2020년 2월 인턴",
        "가상랩 (2022.07~08)",
        "2020-01 - 2021-12 연구원",
    ],
)
def test_period_headers(line: str) -> None:
    _, sections = structure(["경력", line])
    assert sections[0].headers and sections[0].headers[0].period
