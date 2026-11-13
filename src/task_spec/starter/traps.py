"""숨은 테스트 함정 관문(⑪) — 허용 설계의 함정대로 짠 구현이 숨은 테스트에서 떨어지는가 (#171, docs/design/spec-to-code.md §7).

빌드는 숨은 테스트에 대해 ③ 흔한 구현(재료의 naive_impl)이 떨어지는지, ④ 허용 설계 구현이 통과하는지를 본다. 그런데 명세의 허용 설계에는
'함정: 조회한 뒤 없으면 저장'처럼 그 설계를 따르다 빠지기 쉬운 잘못이 적혀 있고, 행동 기준(3점 ↔ 5점)도 그 함정으로 가르는데,
숨은 테스트가 그 함정을 실제로 떨어뜨리는지는 아무도 보지 않았다. 이 관문은:

- 허용 설계마다(함정이 적힌 것만) 그 설계의 구현에서 함정에 빠진 변형을 만든다 (`gen_trap` — 숨은 테스트를 보지 않는 독립 검사).
- 변형마다 시작 코드 + 변형 + 공개 테스트 + 숨은 테스트를 돌린다. 숨은 케이스가 하나라도 떨어지면 잡힘, 숨은 케이스가 모두 통과하면 살아남음,
  컴파일·기동이 안 되거나 공개 테스트에서 떨어지면 무효 (함정 말고 다른 것도 어겼다).
- 살아남으면 그 함정을 붙여 그 숨은 요구의 케이스를 다시 만들고 (회차를 쓴다 — 숨은 테스트가 바뀌면 모든 게이트를 다시 본다),
  그래도 살아남으면 승인자 표시 — 행동 기준의 그 함정을 숨은 테스트가 가르지 못한다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from task_spec.schemas import AcceptedDesign, GeneratedFile, GenTrapOutput, HiddenPlan, VerifyReport
from task_spec.starter.static_check import Overlay
from task_spec.starter.variants import changed_lines, is_fixed_path

MAX_TRAP_LINES = 30  # 함정 변형이 바꾼 줄 상한 — 설계 구조를 바꾸는 함정(조회 뒤 저장 등)이라 1차 변이보다 넉넉히
LABEL = "trap"
GATE = "⑪"
BUDGET = "#trap"  # 숨은 테스트 수리 예산을 다른 게이트와 따로 센다

# '함정: …' — 재료 형식('[복구] 조치 — 조건: … — 함정: …'). 대시·괄호 표기도 받는다 (리뷰 2026-11-13)
_TRAP = re.compile(
    r"함정\s*([:：]|[—–-]+|\()\s*(.+?)(?=\s*(?:조건|함정)\s*(?:[:：]|[—–-]+)|$)", re.S
)


def design_trap(conditions: str) -> str | None:
    """허용 설계 조건에 적힌 함정 문장 (예: '함정: 읽어서 확인한 뒤 따로 UPDATE' → '읽어서 확인한 뒤 따로 UPDATE'). 없으면 None."""
    m = _TRAP.search(conditions or "")
    if m is None:
        return None
    text = " ".join(m.group(2).split()).strip(" —–-")
    if m.group(1) == "(" and text.endswith(")"):  # '함정(…)' 표기 — 닫는 괄호만 뗀다
        text = text[:-1].rstrip()
    return text or None


def unread_traps(designs: Sequence[AcceptedDesign]) -> list[str]:
    """조건에 '함정'이 있는데 문장을 읽지 못한 설계 — 말없이 관문에서 빠지지 않게 표시한다."""
    return [
        d.design_id
        for d in designs
        if "함정" in (d.conditions or "") and design_trap(d.conditions) is None
    ]


@dataclass(frozen=True)
class TrapTarget:
    design_id: str
    summary: str
    conditions: str
    trap: str
    trap_ids: tuple[str, ...]  # 그 설계가 푸는 숨은 요구
    cases: tuple[str, ...]  # 그 숨은 요구를 재는 숨은 케이스


def trap_targets(designs: Sequence[AcceptedDesign], plan: HiddenPlan) -> list[TrapTarget]:
    out: list[TrapTarget] = []
    for d in designs:
        trap = design_trap(d.conditions)
        if trap is None:
            continue
        cases = tuple(c.case_id for c in plan.cases if c.trap_id in d.trap_ids)
        out.append(TrapTarget(d.design_id, d.summary, d.conditions, trap, tuple(d.trap_ids), cases))
    return out


@dataclass(frozen=True)
class TrapSpec:
    label: str
    design_id: str
    trap: str
    note: str
    cases: tuple[str, ...]
    changed: int


def build_traps(
    target: TrapTarget,
    out: GenTrapOutput,
    base: Overlay,
    impl: Mapping[str, str],
    taken: set[str] | None = None,
) -> tuple[list[tuple[Overlay, TrapSpec]], list[str]]:
    """생성 결과 → 실행할 변형 오버레이 (그 설계 구현 파일 + 바꾼 파일). 앱 파일이 아닌 경로·시드·설정 파일, 바뀐 것 없음, 줄 상한을 넘음은 버린다."""
    taken = taken if taken is not None else set()
    kept: list[tuple[Overlay, TrapSpec]] = []
    dropped: list[str] = []
    for v in out.variants:
        why: str | None = None
        changed = 0
        for f in v.files:
            if f.path not in impl:
                why = f"구현에 없는 파일 {f.path}"
                break
            if is_fixed_path(f.path):
                why = f"시드·설정 파일 {f.path} 을 바꿈"
                break
            changed += changed_lines(impl[f.path], f.content)
        if why is None and changed == 0:
            why = "구현과 같음"
        if why is None and changed > MAX_TRAP_LINES:
            why = f"바꾼 줄 {changed} > {MAX_TRAP_LINES}"
        if why is not None:
            dropped.append(f"{target.design_id} 함정: {why}")
            continue
        label = f"{LABEL}-{target.design_id}"
        n = 2
        while label in taken:
            label = f"{LABEL}-{target.design_id}-{n}"
            n += 1
        taken.add(label)
        mine = {f.path: f.content for f in v.files}
        files = [
            GeneratedFile(path=f.path, content=mine.pop(f.path, f.content)) for f in base.files
        ]
        files += [GeneratedFile(path=p, content=c) for p, c in mine.items()]
        overlay = Overlay(kind="ref_variant", files=tuple(files), label=label)
        kept.append(
            (
                overlay,
                TrapSpec(label, target.design_id, target.trap, v.note, target.cases, changed),
            )
        )
    return kept, dropped


Verdict = Literal["caught", "survived", "invalid"]


@dataclass(frozen=True)
class TrapResult:
    label: str
    design_id: str
    trap: str
    note: str
    verdict: Verdict
    failed: tuple[str, ...] = ()  # 떨어진 숨은 케이스 (사유)
    detail: str = ""

    def line(self) -> str:
        head = f"{self.design_id} 함정 '{self.trap}' — {self.note}"
        if self.verdict == "caught":
            return f"{head}: 잡힘 ({', '.join(self.failed[:3])})"
        if self.verdict == "invalid":
            return f"{head}: 무효 ({self.detail})"
        return f"{head}: 살아남음 (숨은 테스트 모두 통과)"


def judge_traps(
    report: VerifyReport, specs: Sequence[TrapSpec], weak: Sequence[str] = ()
) -> list[TrapResult]:
    """함정 변형마다 — 그 설계의 숨은 요구 케이스(spec.cases)가 **단언(ASSERT)** 으로 떨어지면 잡힘, 숨은 케이스가 모두 통과하면 살아남음.
    무효: 컴파일·기동 실패, 공개 테스트에서 떨어짐(함정 말고 다른 것도 어김), 하네스 오류만 남(테스트가 판정을 못 함),
    다른 숨은 요구의 케이스만 떨어짐. weak = 채점에서 뺄 케이스(변별하지 못함) — 판정에 쓰지 않는다 (리뷰 2026-11-13)."""
    by_label = {r.target: r for r in report.runs}
    skip = set(weak)
    out: list[TrapResult] = []
    for s in specs:
        run = by_label.get(s.label)

        def bad(detail: str, spec: TrapSpec = s) -> TrapResult:
            return TrapResult(
                spec.label, spec.design_id, spec.trap, spec.note, "invalid", detail=detail
            )

        if run is None:
            out.append(bad("실행 없음"))
            continue
        if run.setup != "ok":
            out.append(bad(run.setup + (f": {run.setup_detail[:120]}" if run.setup_detail else "")))
            continue
        public_failed = [t.name for t in run.results("public") if t.result != "PASS"]
        if public_failed:
            out.append(
                bad(
                    f"공개 테스트에서 떨어짐 ({', '.join(public_failed[:3])}) — 함정 말고 다른 것도 어김"
                )
            )
            continue
        hidden = [t for t in run.results("hidden") if t.name not in skip]
        if not hidden:
            out.append(bad("숨은 테스트 결과 없음"))
            continue
        mine = set(s.cases) or {t.name for t in hidden}
        asserted = [t for t in hidden if t.result == "ASSERT"]
        caught = tuple(
            f"{t.name}" + (f"[{t.reason}]" if t.reason else "") for t in asserted if t.name in mine
        )
        if caught:
            out.append(TrapResult(s.label, s.design_id, s.trap, s.note, "caught", failed=caught))
        elif asserted:
            out.append(
                bad(
                    f"다른 숨은 요구의 케이스만 떨어짐 ({', '.join(t.name for t in asserted[:3])}) — 함정 말고 다른 것도 어김"
                )
            )
        elif any(t.result == "HARNESS_ERR" for t in hidden):
            out.append(bad("숨은 테스트가 하네스 오류로 판정하지 못함"))
        else:
            out.append(TrapResult(s.label, s.design_id, s.trap, s.note, "survived"))
    return out


def untested_designs(results: Sequence[TrapResult], built: Sequence[str]) -> list[str]:
    """만든 변형이 모두 무효라 한 번도 시험되지 않은 설계 — 잡힘·살아남음이 하나도 없다."""
    judged = {r.design_id for r in results if r.verdict in ("caught", "survived")}
    return [d for d in built if d not in judged]


def survivors(results: Sequence[TrapResult]) -> list[TrapResult]:
    return [r for r in results if r.verdict == "survived"]


def trap_note(results: Sequence[TrapResult], specs: Sequence[TrapSpec]) -> str:
    """살아남은 함정 → 숨은 테스트 수리 요청. 케이스 id 를 적어 그 숨은 요구의 케이스만 다시 만들게 한다 (generate.repair_targets).
    변형 코드는 주지 않는다 — 그 구현 하나가 아니라 그 함정에 빠진 구현 일반이 드러나는 결과를 확인하게."""
    cases = {s.label: s.cases for s in specs}
    lines = [
        f"- 숨은 케이스 {', '.join(cases.get(r.label, ())) or '(그 숨은 요구의 케이스)'}: 허용 설계 {r.design_id} 의 함정 "
        f"'{r.trap}' 에 빠진 구현('{r.note}')이 숨은 테스트를 모두 통과했다"
        for r in results
    ]
    return (
        "허용 설계의 함정에 빠진 구현이 숨은 테스트를 통과한다 — 그 함정에서 드러나는 결과(예: 동시에 온 같은 요청에서 기록이 둘·"
        "줄어든 수량이 성공 수와 어긋남)를 확인하는 조건·단언으로 고친다. 허용 설계는 모두 통과해야 한다 — 설계마다 다를 수 있는 값"
        "(성공 응답 수·재시도 횟수 등)이 아니라 모든 허용 설계에서 같은 결과만 확인한다:\n"
        + "\n".join(lines)
    )


@dataclass
class TrapGate:
    """빌드 하나의 함정 관문 상태 — 변형은 그 허용 설계 구현이 바뀔 때만 다시 만든다 (숨은 테스트만 바뀌면 같은 변형을 다시 돌림)."""

    digests: dict[str, str] = field(default_factory=dict)  # 설계 라벨 → 구현 지문
    variants: dict[str, list[tuple[Overlay, TrapSpec]]] = field(
        default_factory=dict
    )  # 설계 라벨 → 변형
    dropped: list[str] = field(default_factory=list)
    results: list[TrapResult] = field(default_factory=list)
    rounds: int = 0
