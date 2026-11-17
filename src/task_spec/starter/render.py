"""시작 코드의 결정적 파일 — LLM 없음 (docs/design/spec-to-code.md §2 render_readme · §3.2).

- `render_readme`: 지원자 README. 템플릿 `README.tpl.md` 의 자리를 승인된 명세 문장·공개 계약·진행 방식으로 채운다.
  숨은 요구·숨김 수용 기준·허용 설계·2단계·자료 본문·추가 요구 답은 **자리가 없다** — 넣을 방법이 없게 만든다 (승인한 문장 = 지원자가 보는 문장).
  문제 분석 판(요청 글이 있는 명세, analysis-stage.md §5.2·§6)은 두 판: 지원자 `README.md` 는 얇은 판 — 상황 자리에 배경 글, '요구 사항'·
  '공개 수용 기준' 절 대신 '요청' 절, 계약은 모양만(B1). 원래 판 `README.full.md`(full=True)는 지금 내용 그대로 — 승인 화면·평가 키트에만.
- `render_params`: HiddenPlan.harness → 채점 조절값 파일 (템플릿 `hidden_layout.params`). 숨은 테스트 생성기가 쓰지 않고 코드가 쓴다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from task_spec.schemas import ContractField, GeneratedFile, HiddenPlan, TaskContract, TaskItem
from task_spec.schemas.template import TableNaming, TemplateManifest
from task_spec.spec.request_note import request_note
from task_spec.starter.code_map import code_map, code_map_markdown, code_map_problems, starter_view
from task_spec.starter.seed_rows import seed_markdown
from task_spec.starter.tables import contract_tables, table_label
from task_spec.starter.templates import Template

README = "README.md"
README_FULL = "README.full.md"  # 원래 판 — 응시용 이미지에 넣지 않는다 (승인 화면·평가 키트)
README_TEMPLATE = "README.tpl.md"
SLOTS = (
    "title",
    "scenario",
    "requirements",
    "acceptance_public",
    "request",
    "contract",
    "deliverables",
    "process",
)
# 얇은 판에서 바꾸는 템플릿 절 — 세 스택 템플릿이 같은 이름을 쓴다 (판을 새로 만들지 않고 코드가 바꾼다)
_REQUIREMENTS_HEAD = "## 요구 사항"
_ACCEPTANCE_HEAD = "## 공개 수용 기준"
REJECTED = (
    "요청을 처리할 수 없음"  # 얇은 판의 오류 응답 — 어느 규칙인지 말하지 않는다 (B1 공통 거절)
)

_SLOT = re.compile(r"\{\{(\w+)\}\}")


class RenderError(ValueError):
    pass


def thin_readme(task: TaskItem) -> bool:
    """지원자 README 를 얇은 판으로 — 문제 분석 판(요청 글이 있는 명세). 요구는 진행자에게 물어 안다 (analysis-stage.md §5.2)."""
    return bool((task.request or "").strip())


def candidate_contract(task: TaskItem, contract: TaskContract) -> TaskContract:
    """지원자에게 보이는 코드(뼈대·앱 계약 타입 주석)에 쓰는 계약 — 얇은 판이면 엔드포인트 요약을 비운다.
    요약에는 규칙이 들어간다(2026-11-17 알파 #006 컨트롤러 주석 '허용되지 않은 전이는 400으로 거절한다. 변경 시도마다 감사 기록을 남긴다').
    채점 도우미·생성기 입력의 계약은 그대로다."""
    if not thin_readme(task):
        return contract
    return contract.model_copy(
        update={"endpoints": [e.model_copy(update={"summary": ""}) for e in contract.endpoints]}
    )


def public_scenario(task: TaskItem) -> str:
    """상황 글 — 기능형 서술 채우기가 끝에 붙인 자료·질문 안내(request_note — 만드는 곳 하나)는 그대로 뗀다 (README 는 '진행 방식' 절에
    따로 쓴다). #184: 정규식으로 다시 떼던 것을 붙인 문장 그대로 — 문장을 바꾸면 붙이는 쪽·떼는 쪽이 함께 바뀐다."""
    text = (task.scenario or "").strip()
    if task.max_requests is not None:
        text = text.removesuffix(
            request_note(task.max_requests, task.max_clarifications).strip()
        ).strip()
    return text


_ENUM = re.compile(r"enum\([^)]*\)")


def _field(f: ContractField, shape_only: bool = False, hide_values: bool = False) -> str:
    """hide_values: 값 목록(enum(…))을 string 으로 — 거절 응답 본문의 사유 값은 진행자 답에만 (B1 사유 값 예외, §4.2)."""
    extra = ", 선택" if not f.required else ""
    note = f" — {f.constraints}" if f.constraints and not shape_only else ""
    kind = _ENUM.sub("string", f.type) if hide_values else f.type
    return f"`{f.name}` ({kind}{extra}){note}"


def _rejected(status: int, cli: bool) -> bool:
    """거절 응답인가 — HTTP 는 400 이상, 명령(CLI)은 0 이 아닌 종료 코드."""
    return status != 0 if cli else status >= 400


def contract_markdown(
    contract: TaskContract,
    *,
    public_tests: bool = False,
    tables: TableNaming | None = None,
    shape_only: bool = False,
) -> str:
    """공개 계약 → README "API 계약" 절 본문. public_tests 는 공개 테스트를 지원자에게 줄 때만 (§4.3 [미정]).
    tables 를 주면 엔티티마다 템플릿 규칙으로 정한 테이블 이름을 적는다 (starter/tables.py).
    shape_only(얇은 판, B1 — analysis-stage.md §5.2): 경로·메서드·칸 이름·형식·상태 값 이름만. 엔드포인트 요약·응답 설명·칸 제약은 빼고,
    거절 응답은 상태 코드마다 한 줄 '요청을 처리할 수 없음' — 본문은 칸 이름·형식만(값 목록 없이). 상태 규칙이 있는 엔티티는 상태 값
    이름만 적는다(허용 전이는 규칙). 화면·설정·입력 환경·시드는 그대로 — 프론트는 B1 대신 요소가 필요 없는 규칙만 묻는다(§5.6)."""
    names = {m.entity_id: m.name for m in contract.data_shapes()}
    table_of = contract_tables(contract, tables)
    out: list[str] = []
    for e in contract.endpoints:
        head = f"{e.method} {e.path}" if e.method else e.path
        out += [f"### `{head}`", ""] + ([] if shape_only else [e.summary, ""])
        if not e.method and (
            e.request or e.query
        ):  # CLI 명령 — 옵션은 --이름 (starter/py_helpers.flag)
            out.append(
                "- 옵션: "
                + " · ".join(
                    _field(f, shape_only).replace(
                        f"`{f.name}`", f"`--{f.name.replace('_', '-')}`", 1
                    )
                    for f in [*e.request, *e.query]
                )
            )
        for label, fields in (
            (("헤더", e.headers), ("쿼리", e.query), ("본문", e.request))
            if e.method
            else (("헤더", e.headers),)
        ):
            if fields:
                out.append(f"- {label}: " + " · ".join(_field(f, shape_only) for f in fields))
        refused: set[int] = set()
        for r in e.responses:
            rejected = shape_only and _rejected(r.status, cli=not e.method)
            if rejected and r.status in refused:
                continue  # 같은 상태 코드의 거절은 한 줄 — 몇 가지 규칙인지도 드러내지 않는다
            if r.returns:
                target = names.get(r.returns.removesuffix("[]"), r.returns)
                shape = f" → `{target}` 목록" if r.returns.endswith("[]") else f" → `{target}`"
            elif r.body:
                shape = " → " + " · ".join(_field(f, shape_only, rejected) for f in r.body)
            else:
                shape = ""
            if rejected:
                refused.add(r.status)
                out.append(f"- `{r.status}` {REJECTED}{shape}")
            else:
                out.append(f"- `{r.status}`{'' if shape_only else ' ' + r.description}{shape}")
        out.append("")
    for sc in contract.screens:
        out += [
            f"### 화면 `{sc.route}`",
            "",
            sc.summary,
            "",
            *([f"화면에 들어오면: {sc.on_load}", ""] if sc.on_load else []),
            "| test id | 역할 | 설명 | 조작 |",
            "|---|---|---|---|",
        ]
        out += [
            f"| `{el.test_id}` | {el.role} | {el.description} | {el.action or ''} |"
            for el in sc.elements
        ]
        if sc.storage:
            out += [
                "",
                "브라우저 저장소: "
                + " · ".join(f"`{k.name}` ({k.kind}) — {k.shape}" for k in sc.storage),
            ]
        out.append("")
    if contract.background:
        out += ["### 화면 조작 없이 나가는 요청", "", contract.background, ""]
    if (
        contract.settings
    ):  # 2026-11-13 #161 — 구현이 읽어야 하는 설정 (채점은 다른 값으로 덮어 본다)
        out += [
            "### 설정",
            "",
            "구현은 이 값을 **설정에서 읽는다** — 코드에 박지 않는다. 채점 테스트는 다른 값으로 덮어 결과가 그 값을 따르는지 본다.",
            "",
            "| 키 | 형식 | 기본값 | 뜻 |",
            "|---|---|---|---|",
        ]
        out += [
            f"| `{st.key}` | {st.type} | {st.default} | {st.meaning} |" for st in contract.settings
        ]
        out.append("")
    out += ["### 데이터", ""]
    for m in contract.entities:
        head = f"#### `{m.name}`"
        if tables is not None and m.entity_id in table_of:
            head += " — " + table_label(table_of[m.entity_id], tables)
        out += [head, "", *_field_table(m.fields, shape_only)]
        if (
            shape_only and m.state is not None
        ):  # 상태 값은 제약 문장에 들어 있어 표에서 빠진다 — 이름만 (전이는 규칙)
            out += [
                "",
                f"상태 값(`{m.state.field}`): " + " · ".join(f"`{v}`" for v in m.state.values),
            ]
        if m.relations:
            out += ["", "관계: " + " · ".join(m.relations)]
        if m.foreign_keys:
            out += ["", "외래 키: " + " · ".join(f"`{a}` → `{b}`" for a, b in m.fk_pairs())]
        out.append("")
    if (
        tables is not None and tables.style != "none"
    ):  # DB 테이블이 없는 템플릿(프론트엔드)에는 외래 키가 없다
        out += ["외래 키는 위 엔티티에 적힌 것만 둔다 (적힌 것이 없으면 두지 않는다).", ""]
    if contract.response_shapes:  # 2026-11-13 — 응답에만 있는 모양 (테이블 아님)
        out += [
            "### 응답 모양",
            "",
            "응답 본문의 모양이다 — 테이블이 아니다 (저장하지 않고 시드도 없다). 칸 값은 제약에 적힌 곳에서 읽거나 계산한다.",
            "",
        ]
        for s in contract.response_shapes:
            out += [f"#### `{s.name}`", "", *_field_table(s.fields, shape_only)]
            out.append("")
    if contract.fixtures:
        out += ["### 입력 환경", ""]
        for fx in contract.fixtures:
            out.append(f"- `{fx.fixture_id}` ({fx.kind}) {fx.summary}")
            if fx.path:
                out.append(f"  - 위치: `{fx.path}`")
            out += [f"  {line}" for line in fx.content.splitlines() if line.strip()]
        out.append("")
    if contract.seed:
        out += ["### 시드 데이터", ""]
        ents = {m.entity_id: m for m in contract.entities}
        out += [
            seed_markdown(s, names.get(s.entity_id, s.entity_id), ents.get(s.entity_id))
            for s in contract.seed
        ]
        out.append("")
    if public_tests:
        out += ["### 공개 테스트", ""]
        out += [f"- {t.test_id}: {t.checks}" for t in contract.public_tests]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _field_table(fields: Sequence[ContractField], shape_only: bool) -> list[str]:
    """필드 표 — 얇은 판은 제약 칸을 뺀다 (제약에 수량 한도·허용 값 같은 규칙이 들어간다)."""
    if shape_only:
        return [
            "| 필드 | 형식 | 필수 |",
            "|---|---|---|",
            *(f"| `{f.name}` | {f.type} | {'예' if f.required else '아니오'} |" for f in fields),
        ]
    return [
        "| 필드 | 형식 | 필수 | 제약 |",
        "|---|---|---|---|",
        *(
            f"| `{f.name}` | {f.type} | {'예' if f.required else '아니오'} | {f.constraints or ''} |"
            for f in fields
        ),
    ]


def _request_sections(tpl: str, name: str) -> str:
    """얇은 판 템플릿 — '요구 사항' 절을 '요청' 절로 바꾸고 '공개 수용 기준' 절을 뺀다 (절 이름이 없으면 RenderError)."""
    parts = re.split(r"(?m)^(?=## )", tpl)
    heads = [p.splitlines()[0].strip() if p else "" for p in parts]
    missing = [h for h in (_REQUIREMENTS_HEAD, _ACCEPTANCE_HEAD) if h not in heads]
    if missing:
        raise RenderError(f"{name} 에 얇은 판으로 바꿀 절이 없음: {', '.join(missing)}")
    out = []
    for head, part in zip(heads, parts, strict=True):
        if head == _REQUIREMENTS_HEAD:
            out.append("## 요청\n\n{{request}}\n\n")
        elif head != _ACCEPTANCE_HEAD:
            out.append(part)
    return "".join(out)


def process_markdown(task: TaskItem) -> str:
    """진행 방식 — 질문 한도·요청할 수 있는 자료 이름(본문·역할은 넣지 않음).
    한도가 없으면(None — 2026-11-16 부터 새로 만드는 기능형, analysis-stage.md §4.4) 없다고 적는다 — 지원자가 한도를 짐작해 묻기를 아끼지 않게."""
    out: list[str] = []
    if task.clarifications:
        if task.max_clarifications:
            out.append(
                f"- 요구사항에 대해 진행자에게 **최대 {task.max_clarifications}번** 질문할 수 있다."
            )
        else:
            out.append("- 요구사항에 대해 진행자에게 질문할 수 있다. 질문 수에는 제한이 없다.")
    if task.artifacts:
        limit = f"최대 {task.max_requests}개" if task.max_requests else "개수 제한 없음"
        out.append(f"- 저장소에 없는 다음 자료는 진행자에게 요청하면 받을 수 있다 ({limit}).")
        out += [f"  - {a.title}" for a in task.artifacts]
    out.append("- AI 도구를 써도 된다.")
    return "\n".join(out)


def render_readme(
    task: TaskItem,
    contract: TaskContract,
    template: Template,
    *,
    public_tests: bool = False,
    starter: Sequence[GeneratedFile] | None = None,
    full: bool = False,
) -> GeneratedFile:
    """README.tpl.md 의 자리를 채운다. 템플릿이 모르는 자리를 쓰거나 값이 비면 RenderError.
    starter(시작 코드 출력)를 주면 '시작 코드' 절(이미 있는 파일·화면 경로·함수 이름 — starter/code_map.py)을 '코드 구조' 절 뒤에
    붙이고, 적힌 이름이 시작 코드와 맞는지 대조한다 (맞지 않으면 RenderError — 코드와 README 가 어긋나면 안 된다).
    문제 분석 판(thin_readme)은 기본이 얇은 판 README.md, full=True 면 원래 판 README.full.md. 예전 판은 full 과 관계없이 지금 그대로."""
    tpl_path = template.root / README_TEMPLATE
    tpl = tpl_path.read_text("utf-8")
    thin = thin_readme(task) and not full
    if thin:
        tpl = _request_sections(tpl, tpl_path.name)
    values = {
        "title": task.title or "",
        # 얇은 판 상황 자리는 배경 글 — 상황 글(scenario)에는 요구 문장이 들어 있다 (2026-11-17 #006: '성공·실패와 관계없이 기록을 남겨야')
        "scenario": (task.background or "").strip() if thin else public_scenario(task),
        "requirements": "\n".join(f"- **{r.req_id}** {r.text}" for r in task.requirements),
        "acceptance_public": "\n".join(f"- {a.text}" for a in task.acceptance_public),
        "request": (task.request or "").strip(),
        "contract": contract_markdown(
            contract,
            public_tests=public_tests,
            tables=template.manifest.naming.table,
            shape_only=thin,
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
    content = _SLOT.sub(lambda m: values[m.group(1)], tpl)
    if starter is not None:
        content = with_code_map(content, starter_view(template.root, starter), contract)
    return GeneratedFile(
        path=README_FULL if full and thin_readme(task) else README, content=content
    )


def with_code_map(
    readme: str, files: Sequence[GeneratedFile], contract: TaskContract | None = None
) -> str:
    """README 에 '시작 코드' 절 — '코드 구조' 절 뒤(없으면 끝)에. 대조가 실패하면 RenderError."""
    section = code_map_markdown(code_map(files, contract))
    if not section:
        return readme
    if "\n## 코드 구조" in readme:
        head, rest = readme.split("\n## 코드 구조", 1)
        nxt = rest.find("\n## ")
        merged = (
            head + "\n## 코드 구조" + rest[:nxt] + "\n\n" + section.rstrip("\n") + "\n" + rest[nxt:]
            if nxt >= 0
            else head + "\n## 코드 구조" + rest.rstrip("\n") + "\n\n" + section
        )
    else:
        merged = readme.rstrip("\n") + "\n\n" + section
    bad = code_map_problems(merged, files)
    if bad:
        raise RenderError("README 시작 코드 절이 시작 코드와 맞지 않음: " + "; ".join(bad[:5]))
    return merged


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
