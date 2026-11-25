"""조절값 성립 검사 — 숨은 계획의 조절값으로 흔한 구현이 의도한 사유로 실제로 떨어질 수 있는지, 템플릿이 사유마다 선언한 식으로 계산한다.

2026-11-10 변별 실패 분석: N+1 재료 계획 23개 중 5개가 '200행 × 쿼리 지연 20ms = 4초 < 요청 한도 30~65초' — 흔한 구현(행마다 쿼리)이
시간 초과로 떨어질 수 없는데, 그 빌드는 흔한 구현을 두 번·숨은 테스트를 한 번 다시 만들고 예산을 다 썼다 (nplus1-d).
사유마다 하네스가 정한 의미라 조건은 템플릿 manifest `failure_reasons[].feasible` 에 한 번만 적는다 — 코드는 식을 계산할 뿐이다.

식: 조절값 이름 · `rows`(그 케이스 data 의 행 수 합) · 숫자 · + - * / · 비교 · and/or/not · max/min. 계획에 없는 조절값은
`default` → `default_expr`(하네스가 값이 없을 때 실제로 쓰는 값 — 식 안의 없는 조절값은 하네스처럼 0) 순으로 채우고, 그래도 없으면
그 조건은 판단하지 않는다.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping

from task_spec.schemas.build import HiddenPlan
from task_spec.schemas.template import TemplateManifest

FUNCS = {"max": max, "min": min}
_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.Compare,
    ast.Gt,
    ast.GtE,
    ast.Lt,
    ast.LtE,
    ast.Eq,
    ast.NotEq,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Call,
)


def parse(expr: str) -> ast.Expression:
    """허용한 꼴만 — 이름·숫자·사칙·비교·and/or/not·max/min. 그 밖은 ValueError (manifest 를 읽을 때 잡는다)."""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _NODES):
            raise ValueError(f"성립 조건 식에 쓸 수 없는 꼴: {type(node).__name__} ({expr})")
        if isinstance(node, ast.Constant) and not isinstance(node.value, int | float):
            raise ValueError(f"성립 조건 식의 값은 숫자만: {expr}")
        if isinstance(node, ast.Call) and not (
            isinstance(node.func, ast.Name) and node.func.id in FUNCS and not node.keywords
        ):
            raise ValueError(f"성립 조건 식의 함수는 max·min 만: {expr}")
    return tree


def names(expr: str) -> set[str]:
    return {n.id for n in ast.walk(parse(expr)) if isinstance(n, ast.Name)} - set(FUNCS)


def evaluate(expr: str, env: Mapping[str, float]) -> float | bool:
    code = compile(parse(expr), "<feasible>", "eval")
    return eval(code, {"__builtins__": {}, **FUNCS}, dict(env))  # noqa: S307 — parse 가 꼴을 막는다


def harness_env(harness: Mapping[str, object], manifest: TemplateManifest) -> dict[str, float]:
    """계획의 조절값 + 빠진 값은 default → default_expr (다른 조절값을 쓰는 식은 채울 수 있을 때까지 되풀이)."""
    env = {
        k: float(v)
        for k, v in harness.items()
        if isinstance(v, int | float) and not isinstance(v, bool)
    }
    for p in manifest.harness_params:
        if p.name not in env and p.default is not None:
            env[p.name] = float(p.default)
    given = dict(
        env
    )  # 식 안의 없는 조절값은 0 — 하네스가 값이 없을 때 계산하는 방식과 같게 (GraderSupport.requestTimeout)
    for p in manifest.harness_params:
        if p.name not in env and p.default_expr:
            zeros = dict.fromkeys(names(p.default_expr) - given.keys(), 0.0)
            env[p.name] = float(evaluate(p.default_expr, given | zeros))
    return env


def _fmt(v: float) -> str:
    return f"{v:g}"


MAX_CASE_ROWS = 2000  # 숨은 케이스 data 한 줄의 행 수 상한 (schemas.build HiddenCaseData.rows)


def fit_feasible_rows(plan: HiddenPlan, manifest: TemplateManifest) -> tuple[HiddenPlan, list[str]]:
    """성립 조건이 행 수(rows)를 쓰는데 성립하지 않으면, 같은 식을 계산해 성립하는 가장 작은 행 수로 그 케이스의 가장 큰 data 줄을 늘린다.
    행 수는 운영 값이 아니라 케이스 안에서 정하는 값이라 코드가 계산한다 — 산수를 LLM 에 다시 묻지 않는다. 상한(MAX_CASE_ROWS) 안에서 못
    맞추면 그대로 둔다 (feasibility_problems 가 다시 묻는다). 2026-11-25 목록 조회 알파: 행 50 × 지연 12ms < 요청 한도 2초를 계약 재요청
    3회 안에 못 맞춰 실패."""
    reasons = {r.code: r for r in manifest.failure_reasons}
    base = harness_env(plan.harness, manifest)
    cases = []
    notes: list[str] = []
    for c in plan.cases:
        reason = reasons.get(c.intended_reason)
        conds = [
            f.when
            for f in (reason.feasible if reason is not None else [])
            if "rows" in names(f.when) and names(f.when) - {"rows"} <= base.keys()
        ]
        rows = sum(d.rows for d in c.data)
        if not conds or not rows:
            cases.append(c)
            continue

        def holds(r: int, conds: list[str] = conds) -> bool:
            env = base | {"rows": float(r)}
            return all(bool(evaluate(w, env)) for w in conds)

        if holds(rows):
            cases.append(c)
            continue
        i = max(range(len(c.data)), key=lambda k: c.data[k].rows)
        room = MAX_CASE_ROWS - c.data[i].rows
        need = next((r for r in range(rows + 1, rows + room + 1) if holds(r)), None)
        if need is None:
            cases.append(c)
            continue
        data = [
            d.model_copy(update={"rows": d.rows + need - rows}) if k == i else d
            for k, d in enumerate(c.data)
        ]
        cases.append(c.model_copy(update={"data": data}))
        notes.append(
            f"숨은 케이스 {c.case_id}({c.intended_reason}) 행 {rows} → {need} — 성립 조건 {', '.join(conds)} "
            "(행 수는 케이스 안에서 정하는 값이라 코드가 맞춤)"
        )
    if not notes:
        return plan, []
    return plan.model_copy(update={"cases": cases}), notes


def feasibility_problems(plan: HiddenPlan, manifest: TemplateManifest) -> list[str]:
    """케이스마다 의도한 사유의 성립 조건을 계산 — 성립하지 않으면 숫자를 붙인 재요청 사유."""
    reasons = {r.code: r for r in manifest.failure_reasons}
    base = harness_env(plan.harness, manifest)
    out: list[str] = []
    for c in plan.cases:
        reason = reasons.get(c.intended_reason)
        if reason is None or not reason.feasible:
            continue
        rows = sum(d.rows for d in c.data)
        env = base | ({"rows": float(rows)} if rows else {})
        for f in reason.feasible:
            needed = names(f.when)
            if not needed <= env.keys():
                continue  # 판단할 값이 없다 (예: 행 수를 정하지 않은 케이스)
            if not evaluate(f.when, env):
                now = ", ".join(f"{n}={_fmt(env[n])}" for n in sorted(needed))
                out.append(
                    f"숨은 케이스 {c.case_id}({c.intended_reason}): 이 조절값으로는 흔한 구현이 이 사유로 떨어지지 않음 — "
                    f"{f.why} (지금 {now}, 조건 {f.when}). 자료가 정한 운영 값은 그대로 두고, 케이스의 행 수나 자료에 근거가 있는 다른 값으로 맞춘다"
                )
    return out
