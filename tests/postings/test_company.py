"""공고 → 회사 (GET /runs 회사 묶기) — 회사 id 규칙·온톨로지에 없는 새 회사. 가상 데이터만."""

from __future__ import annotations

from typing import Any

from task_spec.postings.company import PostingCompany, company_id, posting_companies


def test_company_id_drops_spaces_legal_forms_and_case() -> None:
    assert (
        company_id("가상 회사")
        == company_id("(주)가상회사")
        == company_id("㈜ 가상회사")
        == "가상회사"
    )
    assert company_id("주식회사 가상회사") == "가상회사"
    assert company_id("Fake Corp") == company_id("fakecorp") == "fakecorp"
    assert company_id("(주)") is None and company_id("  ") is None and company_id(None) is None


class _Onto:
    async def fetch(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if "FROM company" in query:
            return [{"name": "가상회사"}, {"name": "FakeCorp"}]
        rows = {"posting-fake-001": "가상회사", "posting-fake-002": "FakeCorp"}
        return [{"id": i, "company": rows[i]} for i in params[0] if i in rows]


class _Store:
    async def get_provisional(self, pid: str) -> dict[str, Any] | None:
        rows: dict[str, str | None] = {
            "prov-fake-a": "fake corp",
            "prov-fake-b": "새 가상회사",
            "prov-fake-c": None,
        }
        return {"company": rows[pid]} if pid in rows else None


async def test_posting_companies_match_ontology_and_keep_new_companies() -> None:
    got = await posting_companies(
        [
            "posting-fake-001",
            "posting-fake-002",
            "posting-fake-404",
            "prov-fake-a",
            "prov-fake-b",
            "prov-fake-c",
            "prov-fake-404",
        ],
        ontology=_Onto(),  # type: ignore[arg-type]
        store=_Store(),
    )
    assert got == {
        "posting-fake-001": PostingCompany("가상회사", "가상회사", True),
        "posting-fake-002": PostingCompany("fakecorp", "FakeCorp", True),
        "prov-fake-a": PostingCompany(
            "fakecorp", "FakeCorp", True
        ),  # 표기가 달라도 온톨로지 회사로
        "prov-fake-b": PostingCompany(
            "새가상회사", "새 가상회사", False
        ),  # 온톨로지에 없는 새 회사
    }  # 없는 공고·회사 칸이 빈 임시 공고는 빠진다


async def test_posting_companies_without_ontology_still_names_provisional() -> None:
    got = await posting_companies(
        ["posting-fake-001", "prov-fake-a"], ontology=None, store=_Store()
    )
    assert got == {"prov-fake-a": PostingCompany("fakecorp", "fake corp", False)}


class _EnteredStore(_Store):
    """사람이 정한 회사가 있는 저장소 — 공고 데이터의 회사보다 먼저."""

    async def entered_companies(self, pids: list[str]) -> dict[str, str]:
        rows = {
            "prov-fake-a": "다른 가상회사",
            "prov-fake-c": "(주)가상회사",
            "posting-fake-002": "가상회사",
        }
        return {p: rows[p] for p in pids if p in rows}


async def test_entered_company_comes_first() -> None:
    """[확정: 2026-11-19 사용자 — 인증 전에는 입력한 회사] 회사 칸이 빈 임시 공고·온톨로지 공고도 정한 회사로."""
    got = await posting_companies(
        ["prov-fake-a", "prov-fake-b", "prov-fake-c", "posting-fake-002"],
        ontology=_Onto(),  # type: ignore[arg-type]
        store=_EnteredStore(),
    )
    assert got["prov-fake-a"] == PostingCompany("다른가상회사", "다른 가상회사", False, "entered")
    assert got["prov-fake-b"] == PostingCompany("새가상회사", "새 가상회사", False, "posting")
    assert got["prov-fake-c"] == PostingCompany("가상회사", "가상회사", True, "entered")
    assert got["posting-fake-002"] == PostingCompany("가상회사", "가상회사", True, "entered")
