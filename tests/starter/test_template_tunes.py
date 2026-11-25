"""빌드가 실험으로 맞출 숨긴 조절값 (#301) — Java 템플릿 v4 의 tunes·대기 한도·지연 기본값, 계약 단계에서 tunes 값 빼기,
재료 안내의 tunes. 가상 데이터만 — 네트워크·빌드 없음 (v4 하네스 자체 점검은 Gradle 로 따로 돌린다, templates/README.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from task_spec.nodes.starter.plan_contract import fit_tuned_params, for_material
from task_spec.schemas import FailureReason, MaterialGuide
from task_spec.schemas.build import HiddenPlan
from task_spec.schemas.incident import PublicIncident
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.feasibility import harness_env
from task_spec.starter.material_guide import guide_problems, with_guide
from task_spec.starter.templates import TEMPLATES_ROOT, check_template, load_template

JAVA = "backend/java-spring-boot3-jpa"
V4 = load_template(JAVA, 4)
INCIDENTS = Path(__file__).parents[2] / "incidents/public_v1"
POOL_TUNES = ["connection_timeout_ms", "query_delay_ms"]


def _incident(name: str) -> PublicIncident:
    return PublicIncident.model_validate(json.loads((INCIDENTS / name).read_text("utf-8")))


def _raw_v4() -> dict[str, Any]:
    return yaml.safe_load((TEMPLATES_ROOT / JAVA / "v4" / "manifest.yaml").read_text("utf-8"))


def _plan(harness: dict[str, object], evidence: dict[str, str] | None = None) -> HiddenPlan:
    return HiddenPlan.model_validate(
        {
            "harness": harness,
            "evidence": evidence or {},
            "cases": [
                {
                    "case_id": "hc1",
                    "ac_id": "h1",
                    "trap_id": "t1",
                    "scenario": "가상 피크 시간 동시 조회",
                    "expect": "모든 요청이 한도 안에 200",
                    "intended_reason": "pool_acquire_timeout",
                    "data": [{"entity_id": "m1", "rows": 300, "burst": True}],
                }
            ],
        }
    )


def _pool_reason(**update: Any) -> FailureReason:
    """재료 안내가 다시 쓴 풀 사유 — 하네스 표기(log_patterns)는 템플릿과 같다."""
    base = next(r for r in V4.manifest.failure_reasons if r.code == "pool_acquire_timeout")
    return base.model_copy(update={"tunes": [], "how": "가상 재료의 피크 재현법", **update})


# ── 템플릿 v4 ─────────────────────────────────────────────────────────


def test_java_v4_hides_the_measurement_knobs_and_names_what_the_build_may_tune() -> None:
    """대기 한도는 숨김·최소 250(Hikari 하한 — 1~249 는 기동 실패)·기본값 없음, 지연 기본값 20, 사유 tunes. 매니페스트만 바뀐 판."""
    m = V4.manifest
    assert m.ref == f"{JAVA}@v4" and check_template(V4) == [] and m.tune_problems() == []
    timeout = m.param("connection_timeout_ms")
    assert timeout is not None and not timeout.visible and timeout.minimum == 250
    assert timeout.default is None and timeout.default_expr is None
    # 재료가 나열하면 보이는 값이 된다 (for_material) — 명세 근거 대조·운영 값 검사는 그대로 쓴다
    assert timeout.spec_patterns and timeout.config_keys
    delay = m.param("query_delay_ms")
    assert delay is not None and not delay.visible
    assert delay.default == 20 and delay.default_expr is None
    assert {r.code: r.tunes for r in m.failure_reasons if r.tunes} == {
        "pool_acquire_timeout": POOL_TUNES,
        "request_timeout": ["query_delay_ms", "request_timeout_s"],
    }
    v3 = load_template(JAVA, 3)
    for sub in ("project", "hidden"):
        assert V4.files(sub) == v3.files(sub)
        for rel in V4.files(sub):
            assert (V4.root / sub / rel).read_bytes() == (v3.root / sub / rel).read_bytes(), rel


def test_java_v4_feasibility_defaults_are_what_the_harness_uses() -> None:
    """성립 검사가 빈 값에 쓰는 값 = 하네스가 실제로 쓰는 값. 2026-11-25 #301 팩트체크: 대기 한도에 default 30000 을 두면
    성립 검사는 요청 한도를 65초로 계산하는데 하네스는 파일에 값이 없을 때 0 으로 계산해 30초였다."""
    env = harness_env({"pool_size": 20, "concurrency": 100}, V4.manifest)
    assert "connection_timeout_ms" not in env
    assert env["query_delay_ms"] == 20 and env["request_timeout_s"] == 30
    support = (V4.root / "hidden/harness/GraderSupport.java").read_text("utf-8")
    assert 'GraderParams.longValue("connection_timeout_ms", 0)' in support
    doc = (V4.root / "hidden/HARNESS.md").read_text("utf-8")
    assert 'GraderParams.longValue("query_delay_ms", 20)' in doc


@pytest.mark.parametrize(
    ("tunes", "why"),
    [
        (["pool_size"], "보이는 조절값"),
        (["no_such_param"], "harness_params 에 없음"),
        (["query_delay_ms", "query_delay_ms"], "겹침"),
    ],
)
def test_tunes_name_only_hidden_params(tunes: list[str], why: str) -> None:
    raw = _raw_v4()
    next(r for r in raw["failure_reasons"] if r["code"] == "pool_acquire_timeout")["tunes"] = tunes
    with pytest.raises(ValidationError, match=why):
        TemplateManifest.model_validate(raw)


def test_tunes_name_only_int_params() -> None:
    raw = _raw_v4()
    raw["harness_params"].append(
        {
            "name": "inject_ids",
            "kind": "int_list",
            "description": "가상 주입 대상",
            "required": False,
        }
    )
    next(r for r in raw["failure_reasons"] if r["code"] == "request_timeout")["tunes"] = [
        "inject_ids"
    ]
    with pytest.raises(ValidationError, match="int 조절값이 아님"):
        TemplateManifest.model_validate(raw)


def test_tunes_name_only_optional_params() -> None:
    """필수 조절값은 계약이 내야 하는 값 — 계약 단계가 tunes 값을 빼면(fit_tuned_params) 계약 검사와 어긋난다."""
    raw = _raw_v4()
    next(p for p in raw["harness_params"] if p["name"] == "query_delay_ms")["required"] = True
    with pytest.raises(ValidationError, match="필수 조절값"):
        TemplateManifest.model_validate(raw)


# ── 계약 단계 ─────────────────────────────────────────────────────────


def test_contract_drops_tuned_hidden_values_the_llm_wrote() -> None:
    """계약 LLM 이 숨긴 보정 값을 넣으면 코드가 뺀다 — 수치는 LLM 이 아니라 실험이 정한다 (#301). 보이는 운영 값·숨은 장치(bursts)는 그대로."""
    plan = _plan(
        {
            "pool_size": 20,
            "concurrency": 100,
            "bursts": 3,
            "connection_timeout_ms": 1000,
            "query_delay_ms": 5,
            "request_timeout_s": 2,
        },
        {"pool_size": "DB 연결 최대값은 20 입니다", "connection_timeout_ms": "없음"},
    )
    fixed, notes = fit_tuned_params(plan, V4.manifest)
    assert fixed.harness == {"pool_size": 20, "concurrency": 100, "bursts": 3}
    assert fixed.evidence == {"pool_size": "DB 연결 최대값은 20 입니다"}
    assert fixed.cases == plan.cases
    assert notes == [
        "빌드가 실험으로 정할 숨긴 조절값 뺌: connection_timeout_ms=1000, query_delay_ms=5, "
        "request_timeout_s=2 (사유의 tunes)"
    ]
    assert fit_tuned_params(fixed, V4.manifest) == (fixed, [])
    # tunes 가 없는 판(CURRENT v2)은 예전 그대로
    assert fit_tuned_params(plan, load_template(JAVA, 2).manifest) == (plan, [])


def test_contract_keeps_tuned_values_the_material_lists() -> None:
    """재료가 나열한 값은 보이는 운영 조건 (for_material) — 명세 근거로 확인한 값이라 빼지 않는다 (목록 조회 재료: 지연·요청 한도)."""
    seed = _incident("sw.backend.n-plus-one.json").feature
    m = for_material(V4, seed).manifest
    assert {p.name for p in m.harness_params if p.visible} >= {
        "query_delay_ms",
        "request_timeout_s",
    }
    plan = _plan({"query_delay_ms": 11, "request_timeout_s": 1})
    assert fit_tuned_params(plan, m) == (plan, [])


# ── 재료 안내 ─────────────────────────────────────────────────────────


def test_guide_redefining_a_reason_keeps_the_template_tunes() -> None:
    """재료가 같은 code 의 사유를 다시 써도 tunes 를 적지 않았으면 템플릿 것을 잇는다 — 보정할 값은 하네스 것이다."""
    guide = MaterialGuide(
        incident_id="pub:sw.backend:pool-hold-and-wait-deadlock", failure_reasons=[_pool_reason()]
    )
    pool = next(
        r
        for r in with_guide(V4, guide).manifest.failure_reasons
        if r.code == "pool_acquire_timeout"
    )
    assert pool.how == "가상 재료의 피크 재현법" and pool.tunes == POOL_TUNES
    own = guide.model_copy(update={"failure_reasons": [_pool_reason(tunes=["query_delay_ms"])]})
    pool = next(
        r for r in with_guide(V4, own).manifest.failure_reasons if r.code == "pool_acquire_timeout"
    )
    assert pool.tunes == ["query_delay_ms"]  # 재료가 적었으면 재료 것


def test_guide_problems_flag_tunes_that_are_not_hidden_params() -> None:
    """합친 사유는 model_copy 라 매니페스트 검증을 다시 거치지 않는다 — 재료 안내 검사가 tunes 를 본다."""
    incident = _incident("sw.backend.json")
    bad = MaterialGuide(
        incident_id=incident.incident_id, failure_reasons=[_pool_reason(tunes=["pool_size"])]
    )
    probs = guide_problems(bad, incident, load=lambda ref: V4)
    assert any("tunes pool_size 이 보이는 조절값" in p for p in probs), probs
    ok = bad.model_copy(update={"failure_reasons": [_pool_reason()]})
    assert guide_problems(ok, incident, load=lambda ref: V4) == []


# ── 템플릿 v5 ─────────────────────────────────────────────────────────


V5 = load_template(JAVA, 5)
# v5 가 v4 와 다른 조절값 설명 — 실험 문구를 연결 대기 사유로 한정(지연)·v3 설명 그대로(요청 한도)
RETOLD = ("query_delay_ms", "request_timeout_s")


def _beside_v5_changes(m: TemplateManifest) -> dict[str, Any]:
    """v5 가 바꾼 칸(판·스택 조건·request_timeout 의 tunes·조절값 설명 둘)을 지운 매니페스트 — 나머지는 v4 와 같아야 한다."""
    d = m.model_dump(exclude={"version", "stack_conditions"})
    for r in d["failure_reasons"]:
        if r["code"] == "request_timeout":
            r["tunes"] = []
    for p in d["harness_params"]:
        if p["name"] in RETOLD:
            p["description"] = ""
    return d


def test_java_v5_adds_the_unique_key_claim_stack_condition() -> None:
    """v5 ① 허용 구현 스택 조건 한 항목 (유일 키 충돌로 중복 막기). 재시도 중복 알파 두 번 모두 허용 구현 d2 가 이 패턴에서 실패했다
    — 11-25 는 직접 정한 @Id 의 save = merge 로 확인, 11-14 는 EntityManager 충돌 예외 종류로 보인다 (로그로 단정하지 않음).
    세 사실을 한 항목에 함께 둔다."""
    assert check_template(V5) == [] and V5.manifest.tune_problems() == []
    extra = [c for c in V5.manifest.stack_conditions if c not in V4.manifest.stack_conditions]
    assert len(extra) == 1
    assert len(V5.manifest.stack_conditions) == len(V4.manifest.stack_conditions) + 1
    for fact in (
        "merge",
        "EntityManager.persist",
        "DataIntegrityViolationException",
        "ConstraintViolationException",
        "새 트랜잭션",
    ):
        assert fact in extra[0], fact


def test_java_v5_tunes_only_what_the_build_calibrates() -> None:
    """v5 ② 빌드 보정에 요청 한도 사다리가 없어 request_timeout 사유의 tunes 를 뺐다 — v4 는 계약이 뺀 요청 한도·지연을 아무도 다시 정하지
    않아 30초·20ms 로 굳었다 (성립에 행 1500개 이상). 요청 한도 설명은 v3 그대로, 지연 설명의 '실험으로 정한다'는 연결 대기 사유로 한정.
    그 밖의 매니페스트와 project/·hidden/ 은 v4 와 같다."""
    m = V5.manifest
    assert {r.code: r.tunes for r in m.failure_reasons if r.tunes} == {
        "pool_acquire_timeout": POOL_TUNES
    }
    for name in RETOLD:
        mine, old = m.param(name), V4.manifest.param(name)
        assert mine is not None and old is not None and mine.description != old.description
    timeout, v3_timeout = (
        m.param("request_timeout_s"),
        load_template(JAVA, 3).manifest.param("request_timeout_s"),
    )
    assert timeout is not None and v3_timeout is not None
    assert timeout.description == v3_timeout.description
    delay = m.param("query_delay_ms")
    assert delay is not None and "pool_acquire_timeout" in delay.description
    assert "실험으로 정한다" in delay.description
    assert _beside_v5_changes(V4.manifest) == _beside_v5_changes(m)
    for sub in ("project", "hidden"):
        assert V5.files(sub) == V4.files(sub)
        for rel in V5.files(sub):
            assert (V5.root / sub / rel).read_bytes() == (V4.root / sub / rel).read_bytes(), rel


def test_v5_contract_keeps_request_timeout_values_a_material_does_not_list() -> None:
    """요청 한도 사유 재료가 값을 나열하지 않으면(FeatureSeed.harness 비움) v4 는 계약 LLM 이 정한 요청 한도·지연을 빼고, v5 는 그대로 둔다.
    재료가 나열하면 두 판 모두 보이는 값이라 빼지 않는다 (test_contract_keeps_tuned_values_the_material_lists)."""
    seed = _incident("sw.backend.n-plus-one.json").feature
    assert seed is not None
    bare = seed.model_copy(update={"harness": []})
    plan = _plan({"query_delay_ms": 5, "request_timeout_s": 1})
    assert fit_tuned_params(plan, for_material(V5, bare).manifest) == (plan, [])
    fixed, notes = fit_tuned_params(plan, for_material(V4, bare).manifest)
    assert fixed.harness == {} and len(notes) == 1
