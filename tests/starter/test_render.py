"""결정적 파일 render_readme · render_params (starter/render.py) — 가상 데이터만, LLM 없음."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from task_spec.schemas import HiddenPlan, TaskContract, TaskItem
from task_spec.starter.render import RenderError, contract_markdown, render_params, render_readme
from task_spec.starter.templates import TEMPLATES_ROOT, load_template
from tests.starter.test_contract import _contract, _plan
from tests.starter.test_plan_contract import _task

JAVA = "backend/java-spring-boot3-jpa"


def _approved() -> TaskItem:
    t = _task()
    return t.model_copy(
        update={
            "scenario": t.scenario
            + "\n\n아래 자료 중 최대 2개를 요청해 볼 수 있고, 질문은 최대 1개까지 할 수 있습니다.",
            "max_requests": 2,
            "max_clarifications": 1,
        }
    )


def _contract_with_list() -> TaskContract:
    c = _contract()
    c["endpoints"][0]["headers"] = [{"name": "X-Actor", "type": "string", "refs": ["r1"]}]
    c["endpoints"][1]["responses"][0] |= {"body": [], "returns": "m2[]"}
    return TaskContract.model_validate(c)


def test_readme_fills_every_slot_from_spec_and_contract() -> None:
    readme = render_readme(_approved(), _contract_with_list(), load_template(JAVA)).content
    assert "{{" not in readme
    assert readme.startswith("# 가상 주문 상태 변경 이력 추가")
    assert "- **r1** 상태 변경 요청마다 이력을 남긴다" in readme
    assert "- 성공한 변경 1건이면 이력 1건" in readme
    assert "### `PATCH /orders/{id}/status`" in readme and "- 헤더: `X-Actor` (string)" in readme
    assert "→ `OrderHistory` 목록" in readme  # returns m2[]
    assert "#### `OrderHistory`" in readme and "| `orderId` | integer | 예 |" in readme
    assert "- `Order` 200행 (id 1~200) — id 1~200: `status`=READY" in readme
    assert "- `OrderHistory` 0행 (처음엔 비어 있음)" in readme
    assert "1. 핵심 구현" in readme
    assert "**최대 1번** 질문할 수 있다" in readme and "(최대 2개)" in readme
    assert "  - 운영 설정" in readme  # 자료 이름만


def test_readme_has_no_hidden_or_material_content() -> None:
    task = _approved()
    readme = render_readme(task, _contract_with_list(), load_template(JAVA)).content
    assert "아래 자료 중 최대" not in readme  # 상황 끝 안내는 진행 방식 절로
    for secret in (
        task.traps[0].description,
        task.acceptance_hidden[0].text,
        task.clarifications[0].answer,
        task.artifacts[0].content,
    ):
        assert secret not in readme, secret
    assert "공개 테스트" not in readme  # 기본은 주지 않음 (§4.3 [미정])


def test_public_tests_listed_only_when_given() -> None:
    md = contract_markdown(_contract_with_list(), public_tests=True)
    assert "### 공개 테스트" in md and "- pt1: 변경 성공 시 이력 1건" in md


def test_template_with_unknown_slot_or_empty_value_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "templates"
    shutil.copytree(TEMPLATES_ROOT / JAVA, root / JAVA)
    tpl = root / JAVA / "v2" / "README.tpl.md"  # CURRENT
    tpl.write_text(tpl.read_text("utf-8") + "\n{{hidden_requirement}}\n", "utf-8")
    with pytest.raises(RenderError, match="hidden_requirement"):
        render_readme(_approved(), _contract_with_list(), load_template(JAVA, root=root))
    no_reqs = _approved().model_copy(update={"requirements": []})
    with pytest.raises(RenderError, match="requirements"):
        render_readme(no_reqs, _contract_with_list(), load_template(JAVA))


def test_render_params_from_hidden_plan() -> None:
    m = load_template(JAVA).manifest
    plan = HiddenPlan.model_validate(
        _plan(harness={"pool_size": 8, "concurrency": 32, "query_delay_ms": 20})
    )
    f = render_params(plan, m)
    assert f.path == "src/test/resources/grader-params.properties"
    assert f.content.splitlines()[1:] == ["pool_size=8", "concurrency=32", "query_delay_ms=20"]
    bad = HiddenPlan.model_validate(_plan(harness={"pool": 8}))
    with pytest.raises(RenderError, match="pool"):
        render_params(bad, m)
