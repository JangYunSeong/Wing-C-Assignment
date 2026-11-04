"""공고 찾기 (GET /postings?q=) — 검색어 처리·준비 안 됐을 때. 실제 검색은 test_postings_pg. 가상 데이터만."""

from __future__ import annotations

from task_spec.postings.search import MAX_TERMS, like_patterns, search_terms


def test_like_patterns_split_and_escape() -> None:
    assert like_patterns("  네이버   백엔드 ") == ["%네이버%", "%백엔드%"]
    assert like_patterns("100%_a\\b") == ["%100\\%\\_a\\\\b%"]  # 와일드카드는 글자 그대로
    assert like_patterns("") == []
    assert len(like_patterns(" ".join(f"t{i}" for i in range(9)))) == MAX_TERMS


def test_search_terms_match_sub_labels() -> None:
    """직무명이 영어("Backend")여도 직군 이름("백엔드·풀스택")으로 찾는다."""
    labels = {
        "sw.backend": "백엔드·풀스택",
        "sw.frontend": "프론트·앱",
        "data.eng": "데이터 엔지니어링",
    }
    t = search_terms("네이버 백엔드", labels)
    assert [x.subs for x in t] == [(), ("sw.backend",)]
    assert search_terms("엔지니어링", labels)[0].subs == ("data.eng",)
    assert search_terms("백엔드")[0].subs == ()  # 직군 목록이 없으면 글자 검색만
