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


async def test_draft_on_real_posting_with_fake_llm(ctx) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    """골격의 실제 수행준거 id 를 인용한 가짜 서술 → 노드가 검증 통과 후 붙인다."""
    import json
    from pathlib import Path

    from task_spec.llm import Completion, FakeBackend, LLMClient
    from task_spec.prompting import PromptRegistry
    from task_spec.spec.common import FLAG_NO_INCIDENT

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
    rel = await load_relatedness(repo)
    skel = (await make_draft_spec(repo, rel)(state))["spec"].common_tasks[0]
    ids = [t.ncs_criterion_ids[0] for t in skel.target_competencies]
    draft = {
        "title": "가상 과제",
        "scenario": "가상의 회사에서 응답 지연이 생겼다.",
        "deliverables": ["원인 분석 메모"],
        "invariant": "원인 구조",
        "variable": ["domain_surface"],
        "traps": [{"trap_id": "t1", "description": "가상 함정", "why_ai_fails": "흔한 답"}],
        "observables": [{"kind": "catches_trap", "value": "t1"}],
        "behavioral_anchors": {
            "level_1": {"behavior": "a", "criterion_ids": ids[:1]},
            "level_3": {"behavior": "b", "criterion_ids": ids},
            "level_5": {"behavior": "c", "criterion_ids": ids[-1:]},
        },
        "tech_used": [],
        # draft_task v3 (자료 점진 공개): 핵심 1·방해 3 이상, 요청 한도 ≤ 자료 수의 3분의 1
        "artifacts": [
            {
                "artifact_id": f"a{i}",
                "title": f"가상 구성 요소 {i} 지표",
                "content": f"가상 로그 {i}",
                "role": r,
            }
            for i, r in enumerate(
                [
                    "key",
                    "support",
                    "distractor",
                    "distractor",
                    "distractor",
                    "support",
                    "support",
                    "support",
                    "support",
                ],
                1,
            )
        ],
        "max_requests": 3,
    }
    fake = FakeBackend().push(Completion(json.dumps(draft, ensure_ascii=False)))
    prompts = PromptRegistry(Path(__file__).resolve().parents[2] / "prompts")
    model = prompts.get("draft_task").meta.model.partition(":")[0]
    out = await make_draft_spec(repo, rel, LLMClient({model: fake}), prompts)(state)
    task = out["spec"].common_tasks[0]
    assert task.title == "가상 과제" and task.behavioral_anchors is not None
    assert task.review_flags[0] == FLAG_NO_INCIDENT and len(fake.calls) == 1
    assert out["errors"] == [] and task.provenance_id == out["provenance"][0].provenance_id
