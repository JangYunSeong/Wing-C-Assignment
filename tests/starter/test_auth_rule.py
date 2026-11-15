"""권한 규칙 칸 — 요청자 행·헤더 값·문장을 코드가, 칸이 필요한지는 응답 코드로 (가상 데이터)."""

from __future__ import annotations

from typing import Any

from task_spec.schemas import HiddenPlan, TaskContract
from task_spec.starter.auth_rule import auth_flags, auth_problems, fit_auth, requester_rows
from task_spec.starter.contract import SpecView, material_enums, material_state_problems
from task_spec.starter.header_values import header_problems, header_values
from task_spec.starter.seed_rows import allocate_rows, public_free, seed_problems
from tests.starter.test_contract import _contract, _plan, _spec

HEADER = "X-Actor"
ROLE = {
    "kind": "role",
    "header": HEADER,
    "denied_status": 403,
    "entity": "m3",
    "identifies": "staffNo",
    "attribute": "role",
    "allowed": ["ADMIN"],
    "same_as_path": ["shopId"],
}


def _shop(auth: dict[str, Any] | None = ROLE, **over: Any) -> dict[str, Any]:
    c = _contract()
    e = c["endpoints"][0]
    e["path"] = "/shops/{shopId}/orders/{id}/status"
    e["headers"] = [{"name": HEADER, "type": "string", "refs": ["r1"]}]
    e["responses"].append({"status": 403, "description": "권한 없음"})
    e["auth"] = auth
    c["entities"].append(
        {
            "entity_id": "m3",
            "name": "Staff",
            "existing": True,
            "fields": [
                {"name": "id", "type": "integer", "refs": ["r1"]},
                {"name": "shopId", "type": "integer", "refs": ["r1"]},
                {"name": "staffNo", "type": "integer", "refs": ["r1"]},
                {"name": "role", "type": "enum(ADMIN|CLERK)", "refs": ["r1"]},
            ],
        }
    )
    c["seed"].append(
        {
            "entity_id": "m3",
            "rows": 20,
            "groups": [{"first": 1, "last": 20, "values": {"shopId": 7, "role": "CLERK"}}],
        }
    )
    c.update(over)
    return c


def _fit(c: dict[str, Any]) -> TaskContract:
    return fit_auth(TaskContract.model_validate(c))[0]


def test_role_rule_adds_requester_rows_and_header_values() -> None:
    fitted, notes = fit_auth(TaskContract.model_validate(_shop()))
    rule = fitted.endpoints[0].auth
    assert rule is not None and rule.rows == [21, 22]
    assert (rule.ok_value, rule.denied_value) == (
        "21",
        "22",
    )  # staffNo 는 values 에 없어 코드 기본값 = 행 id
    staff = next(s for s in fitted.seed if s.entity_id == "m3")
    assert staff.rows == 22 and [g.values for g in staff.groups[-2:]] == [
        {"shopId": 7, "role": "ADMIN"},
        {"shopId": 7, "role": "CLERK"},
    ]
    assert notes and "id 21" in notes[0]
    assert not seed_problems(fitted) and not auth_problems(fitted)
    # 헤더 constraints 는 칸에서 코드가 — 값 예시까지
    assert "role 가 ADMIN 이면 통과" in (fitted.endpoints[0].headers[0].constraints or "")
    assert "통과 예 21, 거절 예 22" in (fitted.endpoints[0].headers[0].constraints or "")
    [h] = header_values(fitted)
    assert (h.ok, h.denied, h.source) == ("21", "22", "권한 규칙")
    assert not header_problems(fitted)
    assert fit_auth(fitted)[0] == fitted  # 두 번 불러도 같다


def test_requester_rows_are_kept_out_of_hidden_and_public_rows() -> None:
    fitted = _fit(_shop())
    assert requester_rows(fitted) == {"m3": {21, 22}}
    plan = HiddenPlan.model_validate(
        _plan(
            cases=[
                {
                    **_plan()["cases"][0],
                    "data": [{"entity_id": "m3", "rows": 12, "initial": {"role": "CLERK"}}],
                }
            ]
        )
    )
    # CLERK 그룹(1~20)에서 작은 id 10행 위로 — 거절용 요청자 행 22(CLERK)는 쓰지 않는다
    alloc = allocate_rows(fitted, plan)
    assert not any(r.last >= 21 for r in alloc.ranges)
    assert not any(
        i in (21, 22) for e, _, ids in public_free(fitted, plan) if e == "m3" for i in ids
    )


def test_auth_is_required_by_denial_not_by_header() -> None:
    missing = auth_problems(TaskContract.model_validate(_shop(auth=None)))
    assert any("auth 칸이 없음" in p for p in missing)
    # 요청자를 기록용으로만 받는 헤더(403 없음)는 칸이 없어도 된다
    c = _shop(auth=None)
    c["endpoints"][0]["responses"] = [
        r for r in c["endpoints"][0]["responses"] if r["status"] != 403
    ]
    assert not auth_problems(TaskContract.model_validate(c))
    # 403 대신 사유 코드로만 말해도 권한 실패
    c["endpoints"][0]["responses"][1]["description"] = "권한 없음 — 기록에 PERMISSION_DENIED"
    assert any("auth 칸이 없음" in p for p in auth_problems(TaskContract.model_validate(c)))
    # 명령·파일 인터페이스는 헤더가 없어 칸을 채울 수 없다 — 사유 코드가 있어도 보지 않는다
    cli = _contract()
    cli["interface_kind"] = "cli"
    for e in cli["endpoints"]:
        e["method"] = None
        e["responses"] = [
            {"status": 0, "description": "성공"},
            {"status": 1, "description": "권한 없는 경로 — PERMISSION_DENIED"},
        ]
    assert not auth_problems(TaskContract.model_validate(cli))


def test_role_rule_problems() -> None:
    bad_field = _fit(_shop({**ROLE, "attribute": "grade"}))
    assert any("attribute grade 가 Staff 필드에 없음" in p for p in auth_problems(bad_field))
    # 식별 열을 상수로 고정하면 요청자를 가리킬 수 없다
    c = _shop()
    c["seed"][-1]["groups"][0]["values"]["staffNo"] = 1
    assert any("identifies staffNo 를 시드 values 에 고정함" in p for p in auth_problems(_fit(c)))
    # allowed 밖 값이 없으면 거절용 행을 못 만든다
    c = _shop({**ROLE, "allowed": ["ADMIN", "CLERK"]})
    assert any("거절용 요청자 행을 만들 수 없음" in p for p in auth_problems(_fit(c)))
    # 범위 필드가 경로 변수에 없음
    c = _shop({**ROLE, "same_as_path": ["staffNo"]})
    assert any("same_as_path staffNo 가 경로 변수에 없음" in p for p in auth_problems(_fit(c)))


def test_fixed_and_other_rules() -> None:
    fixed = _fit(
        _shop({"kind": "fixed", "header": HEADER, "denied_status": 403, "allowed": ["admin"]})
    )
    [h] = header_values(fixed)
    assert (h.ok, h.denied) == ("admin", "guest") and not auth_problems(fixed)
    other = _fit(
        _shop(
            {
                "kind": "other",
                "header": HEADER,
                "denied_status": 403,
                "description": "요청자 등급이 대상보다 높아야",
            }
        )
    )
    assert (
        not auth_problems(other) and auth_flags(other) and "자동 보장 안 됨" in auth_flags(other)[0]
    )
    # other 는 값을 정하지 않으므로 예시가 없으면 헤더 검사가 말하고, 코드가 값을 정하지 않으니 실제 값을 적으라고 짚는다 (fixmt-be2).
    # #181: other 일 때만 붙이던 앞말 갈래 없이 문구 하나
    [msg] = header_problems(other)
    assert "코드가 요청자 값을 정하지 않으므로" in msg and "=1 → 200" in msg
    assert "'통과하는 요청자로' 같은 말 대신" in msg


def test_state_values_follow_the_materials() -> None:
    texts = (
        "grade ENUM('MEMBER','OFFICER','VICE_MASTER','MASTER') NOT NULL",
        "grade: string (enum: MEMBER|OFFICER)",
    )
    assert ["MEMBER", "OFFICER", "VICE_MASTER", "MASTER"] in material_enums(texts)
    c = _contract()
    c["entities"][0]["fields"][1] = {"name": "rank", "type": "string", "refs": ["r1"]}
    c["entities"][0]["state"] = {
        "field": "rank",
        "values": ["GENERAL", "OFFICER", "VICE_MASTER", "MASTER"],
        "transitions": ["GENERAL→OFFICER"],
        "rejected_status": 409,
    }
    spec: SpecView = _spec(visible_texts=texts)
    [p] = material_state_problems(TaskContract.model_validate(c), spec)
    assert "GENERAL" in p and "MEMBER, OFFICER, VICE_MASTER, MASTER" in p
    # 겹침이 적으면 다른 필드의 목록 — 말하지 않는다
    c["entities"][0]["state"]["values"] = ["READY", "SHIPPED", "OFFICER"]
    c["entities"][0]["state"]["transitions"] = ["READY→SHIPPED"]
    assert not material_state_problems(TaskContract.model_validate(c), spec)


def test_rank_order_role_variant() -> None:
    # '요청자 등급이 요청한 등급보다 높아야' — 칸으로 못 적어 other 로 빠지던 규칙 (2026-11-09 fixmt-be2)
    c = _shop(
        {
            **ROLE,
            "allowed": [],
            "order": ["CLERK", "MANAGER", "ADMIN"],
            "above": "requested",
        }
    )
    c["entities"][-1]["fields"][3]["type"] = "enum(ADMIN|MANAGER|CLERK)"
    fitted = _fit(c)
    rule = fitted.endpoints[0].auth
    assert rule is not None and not auth_problems(fitted), auth_problems(fitted)
    staff = next(s for s in fitted.seed if s.entity_id == "m3")
    # 맨 위 등급은 어떤 요청보다 높아 통과, 맨 아래는 어떤 것보다도 높지 않아 거절
    assert [g.values["role"] for g in staff.groups[-2:]] == ["ADMIN", "CLERK"]
    text = fitted.endpoints[0].headers[0].constraints or ""
    assert "요청한 새 값보다 높으면 통과" in text and "CLERK<MANAGER<ADMIN" in text
    # above 없이 order 만, 값 목록 밖 등급은 재요청
    bad = _fit(
        {
            **c,
            "endpoints": [
                dict(
                    c["endpoints"][0],
                    auth={**c["endpoints"][0]["auth"], "above": None, "order": ["CLERK", "BOSS"]},
                ),
                c["endpoints"][1],
            ],
        }
    )
    probs = auth_problems(bad)
    assert any("above(requested·target)도 적는다" in p for p in probs)
    assert any("order 의 BOSS" in p for p in probs)


def test_hidden_case_targets_no_requester_can_change_are_rejected() -> None:
    # 2026-11-10 v19-be2: above=target 인데 hc1 버스트 대상을 맨 위 등급(MASTER)으로 둬 모든 요청이 403 — 숨은 요구를 못 쟀다
    from task_spec.starter.auth_rule import unreachable_targets

    c = _shop({**ROLE, "allowed": [], "order": ["CLERK", "MANAGER", "ADMIN"], "above": "target"})
    c["entities"][-1]["fields"][3]["type"] = "enum(ADMIN|MANAGER|CLERK)"
    fitted = _fit(c)
    plan = _plan()
    plan["cases"][0]["data"] = [
        {"entity_id": "m3", "rows": 5, "initial": {"shopId": 7, "role": "ADMIN"}, "burst": True}
    ]
    [p] = unreachable_targets(fitted, HiddenPlan.model_validate(plan))
    assert "hc1" in p and "role=ADMIN" in p and "CLERK, MANAGER" in p
    plan["cases"][0]["data"][0]["initial"]["role"] = "MANAGER"
    assert not unreachable_targets(fitted, HiddenPlan.model_validate(plan))
    # 같은 등급도 통과(or_equal)면 맨 위도 바꿀 수 있다
    eq = fitted.model_copy(
        update={
            "endpoints": [
                fitted.endpoints[0].model_copy(
                    update={"auth": fitted.endpoints[0].auth.model_copy(update={"or_equal": True})}  # type: ignore[union-attr]
                ),
                *fitted.endpoints[1:],
            ]
        }
    )
    plan["cases"][0]["data"][0]["initial"]["role"] = "ADMIN"
    assert not unreachable_targets(eq, HiddenPlan.model_validate(plan))
    # #181: allowed 로 요청자 상한이 내려가면(MANAGER 까지) 그 위 대상도 닿지 않는다 — 맨 위만 보던 예전 판정은 놓쳤다
    capped = _fit(
        _shop(
            {
                **ROLE,
                "allowed": ["CLERK", "MANAGER"],
                "order": ["CLERK", "MANAGER", "ADMIN"],
                "above": "target",
            }
        )
    )
    plan["cases"][0]["data"][0]["initial"]["role"] = "MANAGER"
    [p] = unreachable_targets(capped, HiddenPlan.model_validate(plan))
    assert "role=MANAGER" in p and "통과용 요청자(role=MANAGER)" in p and "CLERK 중 하나" in p
    plan["cases"][0]["data"][0]["initial"]["role"] = "CLERK"
    assert not unreachable_targets(capped, HiddenPlan.model_validate(plan))
