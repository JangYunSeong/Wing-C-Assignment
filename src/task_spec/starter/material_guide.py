"""재료 숨은 안내 (#256 재료 옮기기) — `incidents/public_v1/hidden/<재료>.yaml` 을 읽고(load_guides) 빌드의 템플릿에 합친다(with_guide).

템플릿과 생성기 프롬프트에는 그 스택에서 늘 맞는 일반 규칙만 두고, 재료에 묶인 사유 정의·판정 문장·예시·허용 구현 조건·공개 테스트
입력 범위는 재료가 가져온다 (schemas.MaterialGuide). 합치는 곳은 하나다 — 계약 단계(plan_contract.for_material)와 빌드(BuildInputs)가
이 함수로 같은 템플릿을 만든 뒤 재료가 고른 사유(FeatureSeed.reasons)로 좁힌다 (templates.restrict_reasons). 그래서 사유를 읽는
곳(생성기 입력·정적 검사·실현 가능성·계약 검사)은 고치지 않아도 재료 정의를 본다.

- 사유: 템플릿 순서를 그대로 두고 같은 code 는 재료 정의로 바꾼다. 재료에만 있는 code 는 뒤에 더한다. 같은 code 의 log_patterns 가
  템플릿과 다르면 거부한다 — 채점 하네스의 사유 표기는 템플릿이 정한다.
- 허용 구현 조건: 템플릿 조건 뒤에 재료 조건 (허용 구현 생성기만 읽는다).
- 글: 숨은 테스트 생성기 입력(hidden_text) · 공개 테스트 생성기 입력(public_text) · 계약 계획 입력(contract_rules_text).
  재료 안내가 없으면 모두 빈 글이라 입력이 예전과 같다.

LLM·네트워크 없음.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import yaml

from task_spec.schemas.incident import MaterialGuide, PublicIncident
from task_spec.schemas.template import FailureReason
from task_spec.starter.feasibility import names
from task_spec.starter.templates import TEMPLATE_FOR_SUB, Template, load_template

FOLDER = (
    "hidden"  # incidents/public_v1/ 아래 — 이름·내용이 숨은 쪽이라 공개 사례 파일(*.json)과 나눈다
)
HIDDEN_TITLE = "## 재료 안내"
PUBLIC_TITLE = "## 재료가 정한 공개 테스트 입력 규칙"


def load_guides(root: Path) -> dict[str, MaterialGuide]:
    """재료 id → 숨은 안내 (`<root>/hidden/*.yaml`). 폴더가 없으면 빈 dict. 같은 재료를 두 파일이 정하면 거부한다."""
    out: dict[str, MaterialGuide] = {}
    folder = root / FOLDER
    if not folder.is_dir():
        return out
    for f in sorted(folder.glob("*.yaml")):
        guide = MaterialGuide.model_validate(yaml.safe_load(f.read_text("utf-8")))
        if guide.incident_id in out:
            raise ValueError(f"재료 {guide.incident_id} 의 숨은 안내가 둘 이상입니다: {f.name}")
        out[guide.incident_id] = guide
    return out


def guide_ref(guide: MaterialGuide | None) -> str | None:
    """재료 숨은 안내 내용 지문 (sha256) — 같은 프롬프트·템플릿 판이라도 이 파일이 바뀌면 생성기 입력이 달라진다. 평가 보고서에 남긴다."""
    if guide is None:
        return None
    return hashlib.sha256(guide.model_dump_json().encode("utf-8")).hexdigest()


def merged_reasons(base: list[FailureReason], guide: MaterialGuide) -> list[FailureReason]:
    """템플릿 사유 + 재료 사유 — 같은 code 는 재료 정의(제자리), 재료에만 있는 code 는 뒤에 (재료 파일 순서)."""
    own = {r.code: r for r in guide.failure_reasons}
    out: list[FailureReason] = []
    for r in base:
        mine = own.pop(r.code, None)
        if mine is not None and mine.log_patterns != r.log_patterns:
            raise ValueError(
                f"재료 {guide.incident_id} 의 사유 {r.code} 표기(log_patterns)가 템플릿과 다름 — 하네스 표기는 템플릿이 정한다"
            )
        if mine is not None and not mine.tunes and r.tunes:
            # 보정할 숨긴 조절값은 하네스 것 — 재료가 사유를 다시 써도 tunes 를 적지 않았으면 템플릿 것을 잇는다 (#301)
            mine = mine.model_copy(update={"tunes": r.tunes})
        out.append(mine or r)
    return [*out, *own.values()]


def with_guide(template: Template, guide: MaterialGuide | None) -> Template:
    """재료 안내를 합친 템플릿 — 사유·허용 구현 조건을 합치고 안내를 붙인다. guide 가 None 이면 그대로.
    몇 번 불러도 같다 (이미 붙은 안내면 그대로). 재료 사유로 좁힌 템플릿(templates.restrict_reasons)에 붙여도 좁힌 코드 밖의 사유는
    들어오지 않는다 — 먼저 합치고 좁힌 것과 같은 목록."""
    if guide is None:
        return template
    if template.guide is not None:
        if template.guide == guide:
            return template
        raise ValueError(
            f"이미 재료 {template.guide.incident_id} 의 안내가 붙은 템플릿에 {guide.incident_id} 를 붙이려 함"
        )
    m = template.manifest
    conditions = [
        *m.stack_conditions,
        *(c for c in guide.accepted_conditions if c not in m.stack_conditions),
    ]
    reasons = merged_reasons(m.failure_reasons, guide)
    if template.narrowed is not None:
        reasons = [r for r in reasons if r.code in template.narrowed]
    manifest = m.model_copy(update={"failure_reasons": reasons, "stack_conditions": conditions})
    return replace(template, manifest=manifest, guide=guide)


def guide_problems(
    guide: MaterialGuide,
    incident: PublicIncident | None,
    load: Callable[[str], Template] = load_template,
) -> list[str]:
    """재료 안내가 그 재료·템플릿과 맞는지 — 빌드 전에 (builder 계약 단계, 단위 테스트). 비면 맞다.
    재료가 없음·기능형 칸 없음, 템플릿과 다른 사유 표기(with_guide 거부), 합친 사유가 재료 feature.reasons 를 다 담지 못함,
    재료 사유의 실현 조건(feasible)이 템플릿에 없는 조절값을 씀 (템플릿 정의는 manifest 검증을 거치지만 재료 사유는 합칠 때 들어온다)."""
    if incident is None or incident.feature is None:
        return [f"재료 안내의 재료 {guide.incident_id} 가 없거나 기능형 칸이 없음"]
    # 빌드는 직군 템플릿(TEMPLATE_FOR_SUB)으로 만든다 — 공고 배정 전(templates 가 빈 평가 전용) 재료도 그 템플릿과 대조한다 (#256 5단계)
    by_sub = TEMPLATE_FOR_SUB.get(incident.sub)
    refs = list(dict.fromkeys([*incident.templates, *([by_sub] if by_sub else [])]))
    out: list[str] = []
    for ref in refs:
        try:
            t = with_guide(load(ref), guide)
        except ValueError as exc:
            out.append(f"{ref}: {exc}")
            continue
        if missing := sorted(set(incident.feature.reasons) - t.manifest.reason_codes):
            out.append(f"{ref}: 재료 사유 {missing} 가 템플릿·재료 안내 어디에도 없음")
        params = {p.name for p in t.manifest.harness_params} | {"rows"}
        for r in guide.failure_reasons:
            for f in r.feasible:
                if extra := sorted(names(f.when) - params):
                    out.append(f"{ref}: 사유 {r.code} 의 실현 조건이 없는 조절값 {extra} 를 씀")
        # 재료 사유의 tunes 도 그 템플릿의 숨긴 int 조절값이어야 한다 (#301 — 합친 사유는 매니페스트 검증을 다시 거치지 않는다)
        out += [f"{ref}: {p}" for p in t.manifest.tune_problems()]
    return out


def reason_descriptions(template: Template, guide: MaterialGuide | None) -> dict[str, str]:
    """실패 사유 코드 → 설명 (평가 키트·채점 화면) — 템플릿 사유 위에 재료 안내의 사유 (빌드가 본 정의와 같게)."""
    out = {r.code: r.description for r in template.manifest.failure_reasons}
    if guide is not None:
        out |= {r.code: r.description for r in guide.failure_reasons}
    return out


def public_rules(template: Template) -> tuple[str, ...]:
    """재료가 정한 공개 테스트 입력 규칙 — 손 구현 관문의 공개 테스트 수리 요청에 붙는다 (build._hand_gate)."""
    return tuple(template.guide.public_rules) if template.guide is not None else ()


def _fence(template: Template) -> str:
    stack = template.manifest.stack
    return stack.language.lower() if stack is not None else ""


def hidden_text(template: Template) -> str:
    """숨은 테스트 생성기 입력의 하네스 문서 뒤에 붙는 '재료 안내' 절 — 판정 문장과 예시. 안내가 없으면 빈 글."""
    g = template.guide
    if g is None or not (g.hidden_rules or g.hidden_example):
        return ""
    parts = [
        f"\n\n{HIDDEN_TITLE}\n\n"
        "이 과제의 재료가 정한 숨은 테스트 규칙이다 — 위 일반 규칙과 함께 지키고, 이 재료의 숨은 요구를 시험하는 법은 이것을 따른다."
    ]
    if g.hidden_rules:
        parts.append("\n\n" + "\n".join(f"- {r}" for r in g.hidden_rules))
    if g.hidden_example:
        parts.append(
            "\n\n### 재료 예시 (모양만 — 계약에 맞춘다)\n\n"
            f"```{_fence(template)}\n{g.hidden_example.strip()}\n```"
        )
    return "".join(parts)


def public_text(template: Template) -> str:
    """공개 테스트 생성기 입력의 공개 테스트 문서 뒤에 붙는 재료의 공개 테스트 입력 규칙. 없으면 빈 글."""
    rules = public_rules(template)
    if not rules:
        return ""
    return (
        f"\n\n{PUBLIC_TITLE}\n\n"
        "공개 테스트 입력은 아래를 지킨다 — 숨은 요구의 장면을 공개 테스트에 담지 않게 (흔한 구현도 공개 테스트는 통과해야 한다).\n\n"
        + "\n".join(f"- {r}" for r in rules)
    )


def contract_rules_text(template: Template) -> str:
    """계약 계획 입력의 재료 블록(interface_text) 뒤에 붙는 공개 테스트 입력 범위 — #256 3단계에 인터페이스 파일에서 렌더하던 것과
    같은 글 (공개 테스트 checks 만 지킨다). 없으면 빈 글."""
    rules = public_rules(template)
    if not rules:
        return ""
    lines = [
        "",
        "- 재료가 정한 공개 테스트 입력 범위 — 공개 테스트 checks 는 아래를 지킨다 (숨은 케이스는 해당 없음).",
        *(f"  - {r}" for r in rules),
    ]
    return "\n".join(lines)
