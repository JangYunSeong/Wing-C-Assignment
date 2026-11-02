"""실제 온톨로지 공고로 공통 과제 골격 (`uv run pytest -m pg`)."""

from __future__ import annotations

import pytest

from task_spec.nodes.spec import make_draft_spec
from task_spec.ontology.queries import criteria_detail, evidence, posting_profile
from task_spec.ontology.relatedness import load_relatedness
from task_spec.schemas import RunState
from tests.postings.test_postings_pg import ctx  # noqa: F401

pytestmark = pytest.mark.pg


async def test_common_skeleton_on_real_posting(ctx) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, repo, _ = ctx
    p = await posting_profile(repo, "kakao_2026#001")
    assert p is not None and p.sub and p.industry_id
    ev = await evidence(
        repo,
        p.competency_ids,
        sub=p.sub,
        industry_id=p.industry_id,
        secondary_sub=p.secondary_sub,
        in_pilot=p.in_pilot,
    )
    state = RunState(
        run_id="r-pg",
        ontology_version=repo.version.version,
        posting_id=p.posting_id,
        posting_profile=p,
        evidence=ev,
    )
    out = await make_draft_spec(repo, await load_relatedness(repo))(state)
    spec = out["spec"]
    assert spec.spec_id == "r-pg-r1" and len(spec.common_tasks) == 1
    task = spec.common_tasks[0]
    assert 2 <= len(task.target_competencies) <= 3
    primary = {c.competency_id for c in p.competencies if c.primary}
    assert {t.competency_id for t in task.target_competencies} <= primary
    for t in task.target_competencies:
        assert t.ncs_unit_ids and t.ncs_criterion_ids
        crit, ksa = await criteria_detail(repo, t.ncs_unit_ids)
        assert (
            {c.unit_cl_cd for c in crit} <= set(t.ncs_unit_ids)
            and len(crit) <= 12
            and len(ksa) <= 10
        )
    assert task.tech_env.basis == "공고 언어" and task.track == "common"
