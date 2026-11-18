"""계약 설정(settings) — 구현이 읽어야 하는 애플리케이션 설정과 그것을 덮어 보는 공개 테스트 (2026-11-13 #161). 가상 데이터만, LLM 없음."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from task_spec.schemas import GeneratedFile, HiddenPlan, TaskContract
from task_spec.starter.contract import SpecView
from task_spec.starter.hidden_helpers import render_contract_case, setting_const, setting_probe
from task_spec.starter.render import contract_markdown
from task_spec.starter.static_check import Overlay, static_check
from task_spec.starter.templates import load_template
from tests.starter.test_contract import BACKEND_SEED, _check, _contract, _manifest, _plan
from tests.starter.test_plan_contract import _task

JAVA = load_template("backend/java-spring-boot3-jpa", 2)
PAGE = {
    "key": "shop.order.page-size",
    "default": 50,
    "meaning": "목록 한 번에 돌려줄 최대 개수",
    "refs": ["r1"],
}


def _with(*settings: dict[str, Any]) -> dict[str, Any]:
    c = _contract()
    c["settings"] = list(settings)
    return c


def test_setting_schema_rejects_bad_key_and_duplicates() -> None:
    TaskContract.model_validate(_with(PAGE))
    with pytest.raises(ValidationError):
        TaskContract.model_validate(_with({**PAGE, "key": "pageSize"}))  # 점 표기 스프링 속성만
    with pytest.raises(ValidationError):
        TaskContract.model_validate(_with(PAGE, {**PAGE, "meaning": "다른 뜻"}))  # 같은 키 둘
    with pytest.raises(ValidationError):
        TaskContract.model_validate(_with({**PAGE, "refs": ["t1"]}))  # 공개 id 만


def test_probe_differs_from_default() -> None:
    assert setting_probe(50) == 3
    assert setting_probe(3) == 2  # 기본값과 같으면 덮어도 가를 수 없다
    assert setting_const("shop.order.page-size") == "SHOP_ORDER_PAGE_SIZE"


def _setting_problems(contract: dict[str, Any], **kw: Any) -> list[str]:
    return [p for p in _check(contract, **kw).problems if "설정" in p or "settings" in p]


def test_contract_check_refs_and_template() -> None:
    assert not _setting_problems(_with(PAGE), manifest=JAVA.manifest)
    bad = _setting_problems(_with({**PAGE, "refs": ["r9"]}), manifest=JAVA.manifest)
    assert any("설정 shop.order.page-size 의 refs 에 명세에 없는 id: r9" in p for p in bad)
    # 스프링(Java) 템플릿이 아니면 설정 덮어쓰기를 쓸 수 없다 (테스트 매니페스트는 스택 없음)
    assert any(
        "settings 는 스프링 템플릿에서만" in p
        for p in _setting_problems(_with(PAGE), manifest=_manifest())
    )


def test_helper_constants_for_both_audiences() -> None:
    contract = TaskContract.model_validate(_with(PAGE))
    plan = HiddenPlan.model_validate(_plan())
    for audience in ("hidden", "public"):
        helper = render_contract_case(contract, plan, JAVA.manifest, audience=audience)  # type: ignore[arg-type]
        assert helper is not None
        assert 'SETTING_SHOP_ORDER_PAGE_SIZE_KEY = "shop.order.page-size";' in helper.content
        assert "SETTING_SHOP_ORDER_PAGE_SIZE_DEFAULT = 50;" in helper.content
        assert "SETTING_SHOP_ORDER_PAGE_SIZE_PROBE = 3;" in helper.content
    no = render_contract_case(TaskContract.model_validate(_contract()), plan, JAVA.manifest)
    assert no is not None and "SETTING_" not in no.content


def test_readme_shows_settings_table() -> None:
    md = contract_markdown(TaskContract.model_validate(_with(PAGE)))
    assert "### 설정" in md and "설정에서 읽는다" in md
    assert "| `shop.order.page-size` | int | 50 | 목록 한 번에 돌려줄 최대 개수 |" in md
    assert "### 설정" not in contract_markdown(TaskContract.model_validate(_contract()))


LEAK = "숨은 요구·운영 조건을 드러내는 말이 공개 계약"


def test_hidden_terms_in_setting_key_and_meaning_are_reasked() -> None:
    """#232: 설정 키·뜻은 README 설정 표로 그대로 나가는데 계약 누설 검사(contract_places)가 보지 않았다 — 같은 말을 엔드포인트
    요약에 두면 재요청 사유였다. 케밥 키(N+1 재료의 'batch size' ← shop.order.batch-size)도."""
    from task_spec.schemas import PublicIncident
    from tests.starter.test_contract import ROOT

    def leaks(contract: dict[str, Any], **kw: Any) -> list[str]:
        return [p for p in _check(contract, manifest=JAVA.manifest, **kw).problems if LEAK in p]

    assert not leaks(_with(PAGE))
    got = leaks(_with({**PAGE, "meaning": "커넥션 풀 크기에 맞춘 목록 한 번에 돌려줄 최대 개수"}))
    assert len(got) == 1 and "커넥션 풀" in got[0] and "설정 shop.order.page-size" in got[0]
    raw = (ROOT / "incidents" / "public_v1" / "sw.backend.n-plus-one.json").read_text("utf-8")
    nplus1 = PublicIncident.model_validate_json(raw).feature
    batch = {**PAGE, "key": "shop.order.batch-size", "meaning": "한 번에 읽어 오는 연관 행 수"}
    got = leaks(_with(batch), seed=nplus1)
    assert len(got) == 1 and "batch size" in got[0] and "설정 shop.order.batch-size" in got[0]


def _static(starter_yml: str, public_test: str) -> list[str]:
    contract = TaskContract.model_validate(_with(PAGE))
    plan = HiddenPlan.model_validate(_plan())
    overlays = [
        Overlay(
            kind="starter",
            files=(GeneratedFile(path="src/main/resources/application.yml", content=starter_yml),),
        ),
        Overlay(
            kind="public_test",
            files=(
                GeneratedFile(
                    path="src/test/java/com/example/service/publictests/Pt1ListTest.java",
                    content=public_test,
                ),
            ),
        ),
    ]
    res = static_check(
        overlays,
        template=JAVA,
        plan=plan,
        spec=SpecView.from_task(_task()),
        contract=contract,
        seed=BACKEND_SEED,
    )
    return [p.message for p in res.problems]


OVERRIDE = """class Pt1ListTest extends PublicCase {
    @DynamicPropertySource
    static void pageSize(DynamicPropertyRegistry r) { r.add(SETTING_SHOP_ORDER_PAGE_SIZE_KEY, () -> SETTING_SHOP_ORDER_PAGE_SIZE_PROBE); }
    void 설정한_개수만큼() { assertThat(rows).hasSize(SETTING_SHOP_ORDER_PAGE_SIZE_PROBE); }
}"""
DEFAULT_ONLY = """class Pt1ListTest extends PublicCase {
    void 기본_개수만큼() { assertThat(rows).hasSize(50); }
}"""
YML = "spring:\n  application:\n    name: service\nshop:\n  order:\n    page-size: 50\n"


def test_static_check_starter_config_and_public_override() -> None:
    ok = _static(YML, OVERRIDE)
    assert not [m for m in ok if "shop.order.page-size" in m]
    missing = _static("spring:\n  application:\n    name: service\n", OVERRIDE)
    assert any("계약 설정 shop.order.page-size 가 시작 코드 설정 파일" in m for m in missing)
    other = _static(YML.replace("50", "20"), OVERRIDE)
    assert any("shop.order.page-size=20 이 계약 기본값 50 와 다름" in m for m in other)
    default_only = _static(YML, DEFAULT_ONLY)  # 기본값만 단언 — 값을 박은 구현도 통과
    assert any(
        "설정 shop.order.page-size 를 따르는지 보는 공개 테스트가 없음" in m for m in default_only
    )
