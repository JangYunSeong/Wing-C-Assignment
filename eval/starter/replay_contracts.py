"""회귀 검사 — 저장한 실제 계약으로 LLM 없이 코드 단계만 다시 돌린다 (몇 초).

알파 한 번(25분)으로만 코드 수정을 확인하다 보니, 앞 단계가 막혀 있던 동안 뒤 단계 코드 버그가 실제 계약으로 실행되지 않았고
루프가 끝까지 돌기 시작하자 하나씩 드러났다 (2026-11-09: fit_seed 가 덧붙인 그룹의 guildId 누락, 글자 경로 키 형식 짐작).
가상 계약(단위 테스트)은 실제 모양(글자 키·범위 필드·덧붙인 그룹)을 다 덮지 못한다.

입력: data/eval/build/<실행>/<run 8자리>/contract-<n>.json (코드가 맞춘 뒤) · contract-<n>.raw.json (LLM 원본, 있으면).
원본이 있으면 코드 단계(plan_contract.fit_output)를 처음부터 다시 돌리고, 없으면 저장된 맞춘 계약을 그대로 본다.
과제는 data/eval/build/2026-11-07-alpha/input/<run 8자리>.json (공통 과제 — 지원자 문서 없음). 출력은 화면에만.

보는 것:
- 계약 검사(check_contract)의 문제 — 다시 요청할 것
- 테스트 도우미(ContractCase·PublicCase)가 만들어지는지, 글자 경로 키마다 키 값 함수(<필드>Of)가 있는지
- 헤더 상수(통과·거절 값)가 정해지는지
- 그 실행이 남긴 숨은·공개 테스트(loop-1/overlays/hidden·public)가 있으면 지금 정적 검사로 다시 — 키 값·헤더 값을 짐작했는지

사용: uv run python -m eval.starter.replay_contracts [디렉터리 …]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from eval.starter.run_paper import template_for
from task_spec.nodes.starter.plan_contract import fit_output
from task_spec.schemas import GeneratedFile, HiddenPlan, PlanContractOutput, TaskContract, TaskItem
from task_spec.starter.contract import SpecView, check_contract
from task_spec.starter.header_values import header_values
from task_spec.starter.hidden_helpers import key_fields, render_contract_case
from task_spec.starter.static_check import _hand_built_keys, _literal_headers

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "data" / "eval" / "build"
INPUTS = BUILD / "2026-11-07-alpha" / "input"


def _loose(data: dict[str, Any]) -> dict[str, Any]:
    """저장된 맞춘 계약은 코드가 쓴 문장이 붙어 200자를 넘을 수 있다 (파이프라인 안에서는 다시 검증하지 않음) — 읽을 때만 자른다."""
    for e in [*data["entities"], *data["endpoints"]]:
        for f in [*e.get("fields", []), *e.get("headers", [])]:
            if f.get("constraints") and len(f["constraints"]) > 200:
                f["constraints"] = f["constraints"][:200]
    return data


def replay(path: Path) -> dict[str, Any]:
    run8 = path.parent.name
    task = TaskItem.model_validate(json.loads((INPUTS / f"{run8}.json").read_text("utf-8"))["task"])
    sub = (task.source_incident or "::").split(":")[1]
    template = template_for(sub)
    spec = SpecView.from_task(task)
    raw_path = path.with_name(path.name.replace(".json", ".raw.json"))
    if raw_path.is_file():
        raw = PlanContractOutput.model_validate_json(raw_path.read_text("utf-8"))
        out, notes = fit_output(raw, template.manifest, spec)
        contract, plan, source = out.contract, out.hidden_plan, "원본→코드 다시"
    else:
        saved = json.loads(path.read_text("utf-8"))
        contract = TaskContract.model_validate(_loose(saved["contract"]))
        plan = HiddenPlan.model_validate(saved["hidden_plan"])
        notes, source = [], "저장된 맞춘 계약"
    check = check_contract(contract, plan, spec, template.manifest)
    keys = [k for k, _, numeric in key_fields(contract) if not numeric]
    missing: list[str] = []
    for audience in ("hidden", "public"):
        helper = render_contract_case(contract, plan, template.manifest, audience=audience)  # type: ignore[arg-type]
        if helper is None:
            continue
        missing += [f"{audience}:{k}Of" for k in keys if f"{k}Of(" not in helper.content]
    tests: list[str] = []
    for kind, label in (("hidden", "숨은 테스트"), ("public", "공개 테스트")):
        d = path.parent / "loop-1" / "overlays" / kind
        if d.is_dir():
            files = [
                GeneratedFile(path=str(p.relative_to(d)), content=p.read_text("utf-8"))
                for p in sorted(d.rglob("*.java"))
            ]
            tests += _hand_built_keys(files, contract, label) + _literal_headers(
                files, contract, label
            )
    return {
        "contract": str(path.relative_to(BUILD)),
        "tests": tests,
        "source": source,
        "notes": len(notes),
        "problems": check.problems,
        "key_fields": keys,
        "missing_key_helpers": missing,
        "headers": [(h.name, h.ok, h.denied, h.source) for h in header_values(contract)],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="*", help="실행 디렉터리 (기본: data/eval/build 아래 전부)")
    a = ap.parse_args()
    roots = [Path(d).resolve() for d in a.dirs] or [BUILD]
    paths = sorted(
        p
        for r in roots
        for p in r.rglob("contract-*.json")
        if not p.name.endswith(".raw.json") and (INPUTS / f"{p.parent.name}.json").is_file()
    )
    bad = 0
    for p in paths:
        try:
            r = replay(p)
        except Exception as exc:  # 계약 하나가 깨져도 나머지는 본다
            bad += 1
            print(f"✗ {p.relative_to(BUILD)}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        flag = "✗" if r["problems"] or r["missing_key_helpers"] or r["tests"] else "✓"
        bad += flag == "✗"
        print(
            f"{flag} {r['contract']} [{r['source']}] 문제 {len(r['problems'])} · 글자 키 {r['key_fields'] or '-'}"
            f" · 키 함수 빠짐 {r['missing_key_helpers'] or '-'}"
        )
        for p_ in r["problems"]:
            print(f"    - {p_[:220]}")
        for p_ in r["tests"]:
            print(f"    - (생성된 테스트) {p_[:220]}")
        for name, ok, denied, src in r["headers"]:
            print(f"    헤더 {name}: 통과 {ok} · 거절 {denied} ({src})")
    print(f"\n계약 {len(paths)}개 중 문제 있음 {bad}")


if __name__ == "__main__":
    main()
