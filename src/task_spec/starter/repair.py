"""검증 실패 → 원인 생성기 배정 (docs/design/spec-to-code.md §7) — 결정적, LLM 없음.

verifier 결과(`VerifyReport`)를 보고 "어느 산출물 탓인가"를 정해 그 생성기만 다시 만들게 한다. 다시 만들 때 붙이는 수정 요청(`note`)에는
실패 사유(게이트·케이스 id·사유 코드·실패 메시지 앞부분·컴파일 오류 위치)만 넣고 다른 산출물의 본문은 넣지 않는다.

판정 순서 — 앞 단계가 깨지면 뒤 단계 결과는 믿을 수 없어 보지 않는다:
1. 시작 코드 (①빌드 · ②기존 테스트) — 실패면 `gen_starter` 만. 시작 코드를 다시 만들면 참조 구현도 다시 (루프가 처리).
2. 참조 구현 대상마다 준비 — 컴파일 오류는 오류가 난 파일의 주인에게 (숨은 테스트 파일 → `gen_hidden`, 그 밖 → 그 참조 구현),
   기동 실패 → 그 참조 구현, 시간 초과(⑨) → `gen_hidden`.
3. 참조 구현이 시작 코드에서 통과하던 기존 테스트를 깨뜨림 → 그 참조 구현 (§7 표 ⑤ 는 `gen_starter`(누설)이지만 2026-11-07 스모크에서는
   흔한 구현이 필수 필드를 빠뜨린 결함이었다 — 참조 구현을 먼저 다시 만들고, 그래도 깨면 누설 의심으로 남긴다).
4. 숨은 케이스 `HARNESS_ERR`(테스트 자체 예외)·결과 없음 → `gen_hidden`.
5. ③ 흔한 구현: 놓친 숨은 요구의 케이스는 의도한 사유로 실패해야 한다. 통과 → `gen_hidden`(약함), 다른 사유·사유 없음 → `gen_hidden`
   (2026-11-07 스모크: "실패 기록 있음"을 "성공 기록 없음"보다 먼저 확인해 의도한 사유 대신 `record_missing`). ③' 지킨 요구의 케이스에서 실패 → `gen_hidden`.
6. ④ 허용 구현: 허용 구현이 둘 이상이고 모두 같은 케이스에서 실패 → `gen_hidden`(너무 엄격). 일부만 실패, 또는 허용 구현이 하나뿐이면
   그 구현을 먼저 (`gen_accepted`) — 이미 다시 만들었는데 또 실패하면 허용 구현이 하나뿐일 때만 `gen_hidden` 으로 넘기고, 아니면 설계 조건 부족(배정 못 함).
⑤ 공개 테스트 누설·⑧ 반복은 이 단계 밖 (공개 테스트 [미정] §4.3, 반복은 §7.1 2단계).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from task_spec.schemas import HiddenPlan, TargetRun, VerifyReport
from task_spec.schemas.build import GeneratorName
from task_spec.starter.static_check import Overlay

MAX_MESSAGE = 220
STARTER = "starter"


@dataclass(frozen=True)
class RepairAction:
    generator: GeneratorName
    label: str  # 오버레이 라벨 — 시작 코드·숨은 테스트는 "", 참조 구현은 naive-all · d1 …
    gate: str  # ① ② ③ ③' ④ ⑤ ⑨ setup
    reason: str  # 짧은 사유 (RepairRecord.reason, 승인 화면)
    note: str  # 생성기에 붙일 수정 요청

    @property
    def key(self) -> str:
        return f"{self.generator}:{self.label}" if self.label else self.generator


@dataclass
class RepairPlan:
    actions: list[RepairAction] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)  # 배정 못 한 실패 → failed + review_flags

    @property
    def passed(self) -> bool:
        return not self.actions and not self.unresolved


def _short(text: str) -> str:
    one = " ".join(text.split())
    return one if len(one) <= MAX_MESSAGE else one[: MAX_MESSAGE - 1] + "…"


def _owner(path: str, run: TargetRun, overlays: Mapping[str, Overlay]) -> str:
    """컴파일 오류가 난 파일의 주인 — 숨은 테스트 파일이면 hidden, 아니면 그 대상(참조 구현)."""
    hidden = overlays.get("hidden")
    if hidden is not None and any(f.path == path for f in hidden.files):
        return "hidden"
    return run.target


def _gen_for(run: TargetRun) -> GeneratorName:
    return "gen_naive" if run.kind == "ref_naive" else "gen_accepted"


def _merge(actions: list[RepairAction]) -> list[RepairAction]:
    """같은 생성기·라벨에 여러 사유가 붙으면 하나로 (사유·수정 요청을 잇는다)."""
    out: dict[str, RepairAction] = {}
    for a in actions:
        prev = out.get(a.key)
        if prev is None:
            out[a.key] = a
        else:
            out[a.key] = RepairAction(
                a.generator,
                a.label,
                prev.gate if prev.gate == a.gate else f"{prev.gate}{a.gate}",
                _short(f"{prev.reason}; {a.reason}"),
                f"{prev.note}\n{a.note}",
            )
    return list(out.values())


def _starter(run: TargetRun) -> list[RepairAction]:
    if run.setup != "ok":
        errs = "\n".join(f"- {e.path}:{e.line} {e.message}" for e in run.compile_errors[:12])
        return [
            RepairAction(
                "gen_starter",
                "",
                "①",
                f"시작 코드 빌드 실패 ({run.setup})",
                "시작 코드만으로 빌드·기동이 되지 않는다. 이 오류를 고친다:\n"
                + (errs or run.setup),
            )
        ]
    bad = [t for t in run.results("existing") if t.result != "PASS"]
    if not bad:
        return []
    lines = "\n".join(f"- {t.name}: {_short(t.message)}" for t in bad)
    return [
        RepairAction(
            "gen_starter",
            "",
            "②",
            "시작 코드의 기존 테스트 실패: " + ", ".join(t.name for t in bad),
            "시작 코드에서 기존 기능 테스트가 통과해야 한다. 테스트와 구현이 서로 맞게 고친다 — 테스트들은 같은 DB 를 쓰고 "
            "실행 순서가 정해지지 않으므로 테스트 메서드마다 서로 다른 시드 행을 쓰는지도 본다:\n"
            + lines,
        )
    ]


def _ref_setup(run: TargetRun, overlays: Mapping[str, Overlay]) -> list[RepairAction]:
    if run.setup == "timeout":
        return [
            RepairAction(
                "gen_hidden",
                "",
                "⑨",
                f"{run.target} 대상이 시간 초과",
                f"숨은 테스트 실행이 시간 상한을 넘었다 ({run.target}). 요청 수·대기 시간을 조절값 안에서 줄인다.",
            )
        ]
    if run.setup == "boot_error":
        return [
            RepairAction(
                _gen_for(run),
                run.target,
                "setup",
                f"{run.target} 기동 실패",
                "구현을 넣은 앱이 기동하지 않는다 — 시작 코드의 설정·빈 구성을 깨뜨리지 않는다.",
            )
        ]
    if run.setup != "compile_error":
        return []
    by_owner: dict[str, list[str]] = {}
    for e in run.compile_errors:
        by_owner.setdefault(_owner(e.path, run, overlays), []).append(
            f"- {e.path}:{e.line} {e.message}"
        )
    out: list[RepairAction] = []
    for owner, errs in by_owner.items():
        text = "\n".join(errs[:12])
        if owner == "hidden":
            out.append(
                RepairAction(
                    "gen_hidden",
                    "",
                    "setup",
                    "숨은 테스트 컴파일 오류",
                    "숨은 테스트가 컴파일되지 않는다. 하네스 문서에 있는 이름·형식만 쓴다:\n"
                    + text,
                )
            )
        else:
            out.append(
                RepairAction(
                    _gen_for(run),
                    run.target,
                    "setup",
                    f"{run.target} 컴파일 오류",
                    "구현이 컴파일되지 않는다:\n" + text,
                )
            )
    if not run.compile_errors:  # 위치를 못 뽑은 컴파일 실패 — 참조 구현 쪽으로
        out.append(
            RepairAction(
                _gen_for(run),
                run.target,
                "setup",
                f"{run.target} 컴파일 실패",
                "구현이 컴파일되지 않는다.",
            )
        )
    return out


def _ref_existing(run: TargetRun, starter: TargetRun) -> list[RepairAction]:
    passed = {t.name for t in starter.results("existing") if t.result == "PASS"}
    broke = [t for t in run.results("existing") if t.result != "PASS" and t.name in passed]
    if not broke:
        return []
    lines = "\n".join(f"- {t.name}: {_short(t.message)}" for t in broke)
    return [
        RepairAction(
            _gen_for(run),
            run.target,
            "⑤" if run.kind == "ref_naive" else "④",
            f"{run.target} 가 기존 테스트를 깨뜨림: " + ", ".join(t.name for t in broke),
            "시작 코드에서 통과하던 기존 기능 테스트가 이 구현에서 실패한다 — 기존 기능·필수 필드를 지킨다:\n"
            + lines,
        )
    ]


def _hidden_broken(run: TargetRun, plan: HiddenPlan) -> list[RepairAction]:
    out: list[RepairAction] = []
    for c in plan.cases:
        r = run.case(c.case_id)
        if r is None:
            out.append(
                RepairAction(
                    "gen_hidden",
                    "",
                    "setup",
                    f"{c.case_id} 결과 없음 ({run.target})",
                    f"숨은 케이스 {c.case_id} 가 실행되지 않았다 — 케이스 파일·표시 이름에 케이스 id 를 쓴다.",
                )
            )
        elif r.result == "HARNESS_ERR":
            out.append(
                RepairAction(
                    "gen_hidden",
                    "",
                    "setup",
                    f"{c.case_id} 테스트 자체 예외 ({run.target})",
                    f"숨은 케이스 {c.case_id} 가 단언이 아닌 예외로 끝났다 ({run.target}): {_short(r.message)}",
                )
            )
    return out


def _gate3(run: TargetRun, plan: HiddenPlan, missed: set[str]) -> list[RepairAction]:
    out: list[RepairAction] = []
    for c in plan.cases:
        r = run.case(c.case_id)
        if r is None or r.result == "HARNESS_ERR":
            continue
        if c.trap_id in missed:
            if r.result == "PASS":
                out.append(
                    RepairAction(
                        "gen_hidden",
                        "",
                        "③",
                        f"흔한 구현이 {c.case_id} 통과 ({run.target})",
                        f"흔한 구현({run.target})이 숨은 케이스 {c.case_id} 를 통과했다 — 숨은 요구 {c.trap_id} 를 어기는 구현이 "
                        f"의도한 사유({c.intended_reason})로 떨어지게 조건·단언을 고친다.",
                    )
                )
            elif r.reason != c.intended_reason:
                got = r.reason or "사유 표시 없음"
                out.append(
                    RepairAction(
                        "gen_hidden",
                        "",
                        "③",
                        f"{c.case_id} 사유 {got} ≠ 의도 {c.intended_reason} ({run.target})",
                        f"숨은 케이스 {c.case_id} 가 흔한 구현에서 의도한 사유({c.intended_reason}) 대신 {got} 로 실패했다: "
                        f"{_short(r.message)} — 의도한 실패를 가장 먼저 확인하는 단언 순서로, 사유는 Reasons.as 로 붙인다.",
                    )
                )
        elif r.result != "PASS":  # ③' 지킨 숨은 요구의 케이스는 통과해야 한다
            out.append(
                RepairAction(
                    "gen_hidden",
                    "",
                    "③'",
                    f"{run.target} 가 지킨 요구의 {c.case_id} 에서 실패",
                    f"숨은 요구 {c.trap_id} 를 지킨 구현({run.target})이 {c.case_id} 에서 실패했다: {_short(r.message)} "
                    "— 케이스가 다른 숨은 요구에 기대지 않게 한다.",
                )
            )
    return out


def _gate4(
    runs: Sequence[TargetRun], plan: HiddenPlan, regen_counts: Mapping[str, int]
) -> tuple[list[RepairAction], list[str]]:
    actions: list[RepairAction] = []
    unresolved: list[str] = []
    for c in plan.cases:
        failed = [
            (r, res)
            for r in runs
            if (res := r.case(c.case_id)) is not None and res.result == "ASSERT"
        ]
        if not failed:
            continue
        labels = ", ".join(r.target for r, _ in failed)
        if len(runs) >= 2 and len(failed) == len(runs):
            actions.append(
                RepairAction(
                    "gen_hidden",
                    "",
                    "④",
                    f"허용 구현 모두 {c.case_id} 실패",
                    f"허용 설계 구현이 모두({labels}) 숨은 케이스 {c.case_id} 에서 실패했다: {_short(failed[0][1].message)} "
                    f"— 허용 설계를 떨어뜨리지 않게 통과 조건({c.expect})만 확인한다.",
                )
            )
            continue
        for r, res in failed:
            key = f"gen_accepted:{r.target}"
            if regen_counts.get(key, 0) == 0:
                actions.append(
                    RepairAction(
                        "gen_accepted",
                        r.target,
                        "④",
                        f"{r.target} 가 {c.case_id} 실패 [{res.reason or '-'}]",
                        f"이 허용 설계 구현이 숨은 요구 {c.trap_id} 의 확인({c.case_id}, 통과 조건: {c.expect})에서 실패했다: "
                        f"{_short(res.message)} — 설계 조건과 스택 조건을 지켜 다시 쓴다.",
                    )
                )
            elif len(runs) == 1 and regen_counts.get("gen_hidden", 0) == 0:
                # 허용 구현이 하나뿐이라 "모두 실패"로 가를 수 없다 — 구현을 한 번 고쳐도 실패하면 테스트를 의심
                actions.append(
                    RepairAction(
                        "gen_hidden",
                        "",
                        "④",
                        f"다시 만든 {r.target} 도 {c.case_id} 실패",
                        f"허용 설계 구현({r.target})을 다시 만들어도 숨은 케이스 {c.case_id} 에서 실패한다: {_short(res.message)} "
                        f"— 허용 설계가 통과할 수 있게 통과 조건({c.expect})만 확인한다.",
                    )
                )
            else:
                unresolved.append(
                    f"허용 설계 {r.target} 가 다시 만든 뒤에도 {c.case_id} 실패 — 설계 조건 부족 또는 숨은 테스트 과함 (사람 확인)"
                )
    return actions, unresolved


def assign(
    report: VerifyReport,
    plan: HiddenPlan,
    overlays: Mapping[str, Overlay],
    missed_traps: Mapping[str, set[str]],
    regen_counts: Mapping[str, int] | None = None,
) -> RepairPlan:
    """검증 결과 → 다시 만들 생성기 목록. overlays 키는 "starter"·"hidden"·참조 구현 라벨.
    missed_traps: 흔한 구현 라벨 → 그 구현이 놓친 숨은 요구 id (naive-all 은 전부)."""
    counts = regen_counts or {}
    res = RepairPlan()
    starter = report.run(STARTER)
    if starter is None:
        res.unresolved.append("시작 코드 검증 결과가 없음")
        return res
    if acts := _starter(starter):
        res.actions = acts
        return res

    actions: list[RepairAction] = []
    ready: list[TargetRun] = []
    for run in report.runs:
        if run.kind == "starter":
            continue
        if run.setup != "ok":
            actions += _ref_setup(run, overlays)
            continue
        actions += _ref_existing(run, starter)
        actions += _hidden_broken(run, plan)
        ready.append(run)
    for run in ready:
        if run.kind == "ref_naive":
            actions += _gate3(run, plan, missed_traps.get(run.target, set()))
    accepted = [r for r in ready if r.kind == "ref_accepted"]
    if accepted:
        acts, unresolved = _gate4(accepted, plan, counts)
        actions += acts
        res.unresolved += unresolved
    res.actions = _merge(actions)
    return res
