"""시작 코드의 결정적 파일 — LLM 없음 (docs/design/spec-to-code.md §2 render_readme · §3.2).

- `render_readme`: 지원자 README. 템플릿 `README.tpl.md` 의 자리를 승인된 명세 문장·공개 계약·진행 방식으로 채운다.
  숨은 요구·숨김 수용 기준·허용 설계·2단계·자료 본문·추가 요구 답은 **자리가 없다** — 넣을 방법이 없게 만든다 (승인한 문장 = 지원자가 보는 문장).
- `render_params`: HiddenPlan.harness → 채점 조절값 파일 (템플릿 `hidden_layout.params`). 숨은 테스트 생성기가 쓰지 않고 코드가 쓴다.
"""

from __future__ import annotations

import json
import re

from task_spec.schemas import ContractField, GeneratedFile, HiddenPlan, TaskContract, TaskItem
from task_spec.schemas.template import TableNaming, TemplateManifest
from task_spec.starter.seed_rows import seed_markdown
from task_spec.starter.tables import contract_tables, table_label
from task_spec.starter.templates import Template

README = "README.md"
README_TEMPLATE = "README.tpl.md"
SLOTS = (
    "title",
    "scenario",
    "requirements",
    "acceptance_public",
    "contract",
    "deliverables",
    "process",
)

_SLOT = re.compile(r"\{\{(\w+)\}\}")
# 기능형 서술 채우기(fill_from_seed_v4)가 상황 끝에 붙이는 안내 — README 는 "진행 방식" 절에 따로 쓴다
_GUIDE = re.compile(
    r"\s*아래 자료 중 최대 \d+개를 요청해 볼 수 있(?:고, 질문은 최대 \d+개까지 할 수 있)?습니다\.\s*\Z"
)


class RenderError(ValueError):
    pass


def public_scenario(task: TaskItem) -> str:
    """상황 글 — 기능형 서술 채우기가 끝에 붙인 자료·질문 안내는 뺀다 (진행 방식은 따로 쓴다)."""
    return _GUIDE.sub("", task.scenario or "").strip()


def _field(f: ContractField) -> str:
    extra = ", 선택" if not f.required else ""
    note = f" — {f.constraints}" if f.constraints else ""
    return f"`{f.name}` ({f.type}{extra}){note}"


def contract_markdown(
    contract: TaskContract, *, public_tests: bool = False, tables: TableNaming | None = None
) -> str:
    """공개 계약 → README "API 계약" 절 본문. public_tests 는 공개 테스트를 지원자에게 줄 때만 (§4.3 [미정]).
    tables 를 주면 엔티티마다 템플릿 규칙으로 정한 테이블 이름을 적는다 (starter/tables.py)."""
    names = {m.entity_id: m.name for m in contract.entities}
    table_of = contract_tables(contract, tables)
    out: list[str] = []
    for e in contract.endpoints:
        head = f"{e.method} {e.path}" if e.method else e.path
        out += [f"### `{head}`", "", e.summary, ""]
        for label, fields in (("헤더", e.headers), ("쿼리", e.query), ("본문", e.request)):
            if fields:
                out.append(f"- {label}: " + " · ".join(_field(f) for f in fields))
        for r in e.responses:
            if r.returns:
                target = names.get(r.returns.removesuffix("[]"), r.returns)
                shape = f" → `{target}` 목록" if r.returns.endswith("[]") else f" → `{target}`"
            elif r.body:
                shape = " → " + " · ".join(_field(f) for f in r.body)
            else:
                shape = ""
            out.append(f"- `{r.status}` {r.description}{shape}")
        out.append("")
    for sc in contract.screens:
        out += [
            f"### 화면 `{sc.route}`",
            "",
            sc.summary,
            "",
            "| test id | 역할 | 설명 |",
            "|---|---|---|",
        ]
        out += [f"| `{el.test_id}` | {el.role} | {el.description} |" for el in sc.elements]
        if sc.storage:
            out += [
                "",
                "브라우저 저장소: "
                + " · ".join(f"`{k.name}` ({k.kind}) — {k.shape}" for k in sc.storage),
            ]
        out.append("")
    out += ["### 데이터", ""]
    for m in contract.entities:
        head = f"#### `{m.name}`"
        if tables is not None and m.entity_id in table_of:
            head += " — " + table_label(table_of[m.entity_id], tables)
        out += [head, "", "| 필드 | 형식 | 필수 | 제약 |", "|---|---|---|---|"]
        out += [
            f"| `{f.name}` | {f.type} | {'예' if f.required else '아니오'} | {f.constraints or ''} |"
            for f in m.fields
        ]
        if m.relations:
            out += ["", "관계: " + " · ".join(m.relations)]
        if m.foreign_keys:
            out += ["", "외래 키: " + " · ".join(f"`{a}` → `{b}`" for a, b in m.fk_pairs())]
        out.append("")
    if tables is not None:
        out += ["외래 키는 위 엔티티에 적힌 것만 둔다 (적힌 것이 없으면 두지 않는다).", ""]
    if contract.fixtures:
        out += ["### 입력 환경", ""]
        for fx in contract.fixtures:
            out.append(f"- `{fx.fixture_id}` ({fx.kind}) {fx.summary}")
            out += [f"  {line}" for line in fx.content.splitlines() if line.strip()]
        out.append("")
    if contract.seed:
        out += ["### 시드 데이터", ""]
        out += [seed_markdown(s, names.get(s.entity_id, s.entity_id)) for s in contract.seed]
        out.append("")
    if public_tests:
        out += ["### 공개 테스트", ""]
        out += [f"- {t.test_id}: {t.checks}" for t in contract.public_tests]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def process_markdown(task: TaskItem) -> str:
    """진행 방식 — 질문 한도·요청할 수 있는 자료 이름(본문·역할은 넣지 않음)."""
    out: list[str] = []
    if task.clarifications:
        limit = f"**최대 {task.max_clarifications}번**" if task.max_clarifications else ""
        out.append(f"- 요구사항에 대해 진행자에게 {limit} 질문할 수 있다.".replace("  ", " "))
    if task.artifacts:
        limit = f" (최대 {task.max_requests}개)" if task.max_requests else ""
        out.append(f"- 저장소에 없는 다음 자료는 진행자에게 요청하면 받을 수 있다{limit}.")
        out += [f"  - {a.title}" for a in task.artifacts]
    out.append("- AI 도구를 써도 된다.")
    return "\n".join(out)


def render_readme(
    task: TaskItem, contract: TaskContract, template: Template, *, public_tests: bool = False
) -> GeneratedFile:
    """README.tpl.md 의 자리를 채운다. 템플릿이 모르는 자리를 쓰거나 값이 비면 RenderError."""
    tpl_path = template.root / README_TEMPLATE
    tpl = tpl_path.read_text("utf-8")
    values = {
        "title": task.title or "",
        "scenario": public_scenario(task),
        "requirements": "\n".join(f"- **{r.req_id}** {r.text}" for r in task.requirements),
        "acceptance_public": "\n".join(f"- {a.text}" for a in task.acceptance_public),
        "contract": contract_markdown(
            contract, public_tests=public_tests, tables=template.manifest.naming.table
        ).strip(),
        "deliverables": "\n".join(f"{n}. {d}" for n, d in enumerate(task.deliverables, 1)),
        "process": process_markdown(task),
    }
    used = set(_SLOT.findall(tpl))
    unknown = sorted(used - set(SLOTS))
    if unknown:
        raise RenderError(f"{tpl_path.name} 에 채울 수 없는 자리: {', '.join(unknown)}")
    empty = sorted(k for k in used if not values[k].strip())
    if empty:
        raise RenderError(f"README 자리에 넣을 명세 문장이 없음: {', '.join(empty)}")
    return GeneratedFile(path=README, content=_SLOT.sub(lambda m: values[m.group(1)], tpl))


def render_params(plan: HiddenPlan, manifest: TemplateManifest) -> GeneratedFile:
    """HiddenPlan.harness → 조절값 파일 (key=value, manifest 순서). 목록은 쉼표로."""
    if manifest.hidden_layout is None:
        raise RenderError(f"{manifest.ref} manifest 에 hidden_layout 이 없음")
    known = [p.name for p in manifest.harness_params]
    unknown = sorted(k for k in plan.harness if k not in known)
    if unknown:
        raise RenderError(f"템플릿에 없는 조절값: {', '.join(unknown)}")
    path = manifest.hidden_layout.params
    # 프론트엔드 템플릿 — 하네스가 JSON 으로 읽는다 (frontend/react-vite-ts grader-params.ts)
    if path.endswith(".json"):
        values = {name: plan.harness[name] for name in known if name in plan.harness}
        return GeneratedFile(
            path=path, content=json.dumps(values, ensure_ascii=False, indent=2) + "\n"
        )
    lines = ["# HiddenPlan.harness — 빌드 파이프라인이 쓴다 (손으로 고치지 않음)"]
    for name in known:
        if name in plan.harness:
            v = plan.harness[name]
            lines.append(f"{name}={','.join(map(str, v)) if isinstance(v, list) else v}")
    return GeneratedFile(path=manifest.hidden_layout.params, content="\n".join(lines) + "\n")
