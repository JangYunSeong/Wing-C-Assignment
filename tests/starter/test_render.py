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


def test_readme_says_there_is_no_limit_when_none() -> None:
    """한도 없음 (analysis-stage.md §4.4, 2026-11-16 부터 새로 만드는 기능형) — 진행 방식에 숫자 대신 제한이 없다고 적는다."""
    task = _approved().model_copy(
        update={
            "max_requests": None,
            "max_clarifications": None,
            "scenario": "가상의 물류 회사가 주문 상태 변경마다 이력을 남기려 한다.",  # 한도 안내 없이 저장됨
        }
    )
    readme = render_readme(task, _contract_with_list(), load_template(JAVA)).content
    assert "질문할 수 있다. 질문 수에는 제한이 없다." in readme
    assert "받을 수 있다 (개수 제한 없음)." in readme and "최대" not in readme


def test_readme_has_no_hidden_or_material_content() -> None:
    task = _approved()
    readme = render_readme(task, _contract_with_list(), load_template(JAVA)).content
    assert "아래 자료 중 최대" not in readme  # 상황 끝 안내는 진행 방식 절로
    # 붙이는 문장을 만드는 곳은 request_note 하나 — 저장된 상황의 문장 그대로(위 픽스처)를 뗀다
    from task_spec.spec.request_note import request_note

    assert task.scenario.endswith(request_note(2, 1))
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


BACKGROUND = "가상의 물류 회사가 주문 상태 변경 기능을 운영한다."
REQUEST = "주문 상태가 바뀐 이력을 남기고 주문마다 볼 수 있게 해 주세요."


def _analysis() -> TaskItem:
    """문제 분석 판 — 배경 글·요청 글이 있는 명세 (analysis-stage.md §4.1, 가상)."""
    return _approved().model_copy(update={"background": BACKGROUND, "request": REQUEST})


def test_analysis_spec_readme_is_thin_and_the_full_one_is_the_original() -> None:
    """지원자 README 는 얇은 판(§5.2·§6) — 요구·공개 수용 기준·계약 규칙은 진행자에게 물어 안다. 원래 판은 예전 README 그대로."""
    task, contract, t = _analysis(), _contract_with_list(), load_template(JAVA)
    thin = render_readme(task, contract, t)
    full = render_readme(task, contract, t, full=True)
    assert (thin.path, full.path) == ("README.md", "README.full.md")
    # 상황 자리는 배경 글 — 상황 글(scenario)에는 요구 문장이 섞인다. 요구·공개 수용 기준 절 대신 요청 절
    assert BACKGROUND in thin.content and _approved().scenario.split("\n")[0] not in thin.content
    assert f"## 요청\n\n{REQUEST}" in thin.content
    assert "## 요구 사항" not in thin.content and "## 공개 수용 기준" not in thin.content
    lines = thin.content.splitlines()
    for r in (
        task.requirements
    ):  # 원래 판이 쓰는 줄 모양 그대로 없는지 (짧은 문장은 제목·배경 글에도 있을 수 있다)
        assert f"- **{r.req_id}** {r.text}" not in lines
    for a in task.acceptance_public:
        assert f"- {a.text}" not in lines
    # 계약은 모양만 — 경로·칸 이름·형식은 남고, 요약·응답 설명·제약은 빠지고, 거절은 공통 문장
    assert "### `PATCH /orders/{id}/status`" in thin.content
    assert "- 본문: `toStatus` (enum(READY|SHIPPED|DELIVERED))" in thin.content
    assert "- `409` 요청을 처리할 수 없음" in thin.content
    assert "| 필드 | 형식 | 필수 |\n" in thin.content and "| 제약 |" not in thin.content
    section = thin.content.split("## API 계약", 1)[1].split("\n## ", 1)[0]
    for e in contract.endpoints:
        assert e.summary not in section
        for r in e.responses:
            assert r.description not in section
    # 원래 판 = 요청 글이 없던 예전 README 와 같은 본문
    old = render_readme(task.model_copy(update={"request": None}), contract, t)
    assert full.content == old.content and old.path == "README.md"


def test_old_spec_has_one_readme_whatever_full_says() -> None:
    """요청 글이 없는 예전 판 — full 을 줘도 지금 README (원래 판이 곧 지원자 README)."""
    t = load_template(JAVA)
    same = render_readme(_approved(), _contract_with_list(), t, full=True)
    assert same == render_readme(_approved(), _contract_with_list(), t)
    assert same.path == "README.md" and "## 요구 사항" in same.content


def test_thin_contract_keeps_shapes_and_hides_values_and_rules() -> None:
    """같은 상태 코드의 거절이 여럿이면(규칙마다 설명이 다른 409) 한 줄 — 몇 가지 규칙인지도 드러내지 않는다.
    거절 본문은 칸 이름·형식만(사유 값 목록은 진행자 답에만), 상태 값 이름은 보이고 허용 전이는 감춘다 (B1 — §5.2 표)."""
    c = _contract()
    body = [
        {"name": "code", "type": "enum(INVALID_TRANSITION|ALREADY_DONE)", "refs": ["r1"]},
        {"name": "message", "type": "string", "refs": ["r1"]},
    ]
    c["endpoints"][0]["responses"] = [
        r | ({"body": body} if r["status"] == 409 else {}) for r in c["endpoints"][0]["responses"]
    ] + [
        {"status": 409, "description": "가상 이미 처리됨", "body": body},
        {"status": 404, "description": "가상 주문 없음"},
    ]
    order = next(m for m in c["entities"] if m["name"] == "Order")
    order["state"] = {
        "field": "status",
        "values": ["READY", "SHIPPED", "DELIVERED"],
        "transitions": ["READY→SHIPPED", "SHIPPED→DELIVERED"],
        "rejected_status": 409,
    }
    md = contract_markdown(TaskContract.model_validate(c), shape_only=True)
    assert md.count("- `409` 요청을 처리할 수 없음") == 1
    assert "- `409` 요청을 처리할 수 없음 → `code` (string) · `message` (string)" in md
    assert "INVALID_TRANSITION" not in md and "가상 이미 처리됨" not in md
    assert "- `404` 요청을 처리할 수 없음" in md
    assert "상태 값(`status`): `READY` · `SHIPPED` · `DELIVERED`" in md and "→SHIPPED" not in md


def test_thin_readme_needs_both_template_sections(tmp_path: Path) -> None:
    root = tmp_path / "templates"
    shutil.copytree(TEMPLATES_ROOT / JAVA, root / JAVA)
    tpl = root / JAVA / "v2" / "README.tpl.md"
    tpl.write_text(tpl.read_text("utf-8").replace("## 공개 수용 기준", "## 수용 기준"), "utf-8")
    with pytest.raises(RenderError, match="공개 수용 기준"):
        render_readme(_analysis(), _contract_with_list(), load_template(JAVA, root=root))


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


def test_guide_sentence_is_stripped_by_what_was_appended(monkeypatch: pytest.MonkeyPatch) -> None:
    """#184: README 가 상황 끝 안내를 정규식으로 떼어, 문장을 한 글자만 바꿔도 README 에 안내가 두 번 나왔다 — 붙이는 쪽과 떼는 쪽이
    같은 함수를 쓰므로 문장을 바꾸면 함께 바뀐다."""
    import task_spec.starter.render as render
    from task_spec.spec.request_note import request_note

    def reworded(n: int, q: int | None = None) -> str:
        return request_note(n, q).replace("있고,", "있으며,").replace("습니다.", "습니다!")

    monkeypatch.setattr(render, "request_note", reworded)
    t = _task()
    task = t.model_copy(
        update={"scenario": t.scenario + reworded(2, 1), "max_requests": 2, "max_clarifications": 1}
    )
    readme = render_readme(task, _contract_with_list(), load_template(JAVA)).content
    assert "아래 자료 중 최대" not in readme and "(최대 2개)" in readme
