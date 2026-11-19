"""빌더가 명세의 재료(source_incident)를 찾는 규칙 — 공개 사례는 파일, 회사 사례는 DB, 못 찾으면 다른 재료로 바꾸지 않고 실패.

예전에는 id 가 파일에 없으면 그 직군의 다른 사례로 말없이 바꿔 빌드했다 (인터뷰·회사 사례가 그 경우). 가상 데이터만.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from task_spec.builder import steps as steps_mod
from task_spec.builder.runner import StepFailed
from task_spec.schemas import TaskItem
from task_spec.spec.company_incidents import CompanyIncident, InMemoryCompanyIncidents
from tests.spec.test_company_incidents import _company_incident

PUBLIC = Path(__file__).resolve().parents[2] / "incidents" / "public_v1"


def _task(source: str | None) -> TaskItem:
    return TaskItem.model_validate(
        {
            "task_id": "ct-가상",
            "track": "common",
            "target_competencies": [{"competency_id": "comp:a", "label": "가상 역량"}],
            "tech_env": {"basis": "공고 언어"},
            "difficulty_target": "medium",
            "time_limit_min": 60,
            "source_incident": source,
        }
    )


def _steps(tmp_path: Path, company: object | None = None) -> steps_mod.RealSteps:
    return steps_mod.RealSteps(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        PUBLIC,
        tmp_path / "images",
        company_incidents=company,  # type: ignore[arg-type]
    )


async def test_public_and_company_incidents_are_found_by_id(tmp_path: Path) -> None:
    store = InMemoryCompanyIncidents()
    rec: CompanyIncident = await store.submit(
        "가상회사", _company_incident("aaaa00000001"), source={}, created_by="x"
    )
    steps = _steps(tmp_path, store)
    pub = await steps.incident_for(_task("pub:sw.backend:read-modify-write-lost-update"))
    assert pub is not None and pub.incident_id == "pub:sw.backend:read-modify-write-lost-update"
    co = await steps.incident_for(_task(rec.incident_id))
    assert co == rec.incident


async def test_missing_incident_fails_instead_of_switching(tmp_path: Path) -> None:
    steps = _steps(tmp_path, InMemoryCompanyIncidents())
    for sid in ("co:sw.backend:000000000000", "pub:sw.backend:no-such-case"):
        with pytest.raises(StepFailed, match="다른 재료로 바꿔 만들지 않는다"):
            await steps.incident_for(_task(sid))


async def test_unreadable_company_store_fails_with_reason(tmp_path: Path) -> None:
    class Denied:
        async def get(self, incident_id: str) -> None:
            raise PermissionError("가상 권한 없음")

    with pytest.raises(StepFailed, match="db-role.sql"):
        await _steps(tmp_path, Denied()).incident_for(_task("co:sw.backend:aaaa00000001"))


async def test_spec_without_incident_id_has_no_material(tmp_path: Path) -> None:
    assert (
        await _steps(tmp_path).incident_for(_task(None)) is None
    )  # contract 가 직군 단계에서 먼저 멈춘다
