"""검증 실패 → 원인 생성기 배정 (docs/design/spec-to-code.md §7) — 결정적, LLM 없음.

verifier 결과(`VerifyReport`)를 보고 "어느 산출물 탓인가"를 정해 그 생성기만 다시 만들게 한다. 다시 만들 때 붙이는 수정 요청(`note`)에는
실패 사유(게이트·케이스 id·사유 코드·실패 메시지 앞부분·컴파일 오류 위치)만 넣고 다른 산출물의 본문은 넣지 않는다.

판정 순서 — 앞 단계가 깨지면 뒤 단계 결과는 믿을 수 없어 보지 않는다:
1. 시작 코드 (①빌드 · ②기존 테스트) — 실패면 `gen_starter` 만. 시작 코드를 다시 만들면 참조 구현도 다시 (루프가 처리).
   ②' 시작 코드 그대로 + 숨은 테스트(검증기의 `starter_hidden` 실행)가 숨은 케이스를 하나라도 통과 → 새 기능이 이미 들어 있음 → `gen_starter` 만
   (이미 다시 만든 뒤에도 통과하면 표시만).
2. 참조 구현 대상마다 준비 — 컴파일 오류는 오류가 난 파일의 주인에게 (숨은 테스트 파일 → `gen_hidden`, 그 밖 → 그 참조 구현),
   기동 실패 → 그 참조 구현, 시간 초과(⑨) → `gen_hidden`.
3. 참조 구현이 둘 이상이고 모두 같은 기존 테스트를 같은 오류로 깸 → 계약 모호 의심 (다시 만들지 않고 사람 확인).
   참조 구현이 시작 코드에서 통과하던 기존 테스트를 깨뜨림 → 그 참조 구현 (§7 표 ⑤ 는 `gen_starter`(누설)이지만 2026-11-07 스모크에서는
   흔한 구현이 필수 필드를 빠뜨린 결함이었다 — 참조 구현을 먼저 다시 만들고, 그래도 깨면 누설 의심으로 남긴다).
4. 숨은 케이스 `HARNESS_ERR`(테스트 자체 예외)·결과 없음 → `gen_hidden`.
5. ③ 흔한 구현: 놓친 숨은 요구의 케이스는 의도한 사유로 실패해야 한다. 통과 → 흔한 구현을 아직 다시 만들지 않았으면 `gen_naive`
   (설명을 안 따랐을 수 있음 — 그 회차에는 그 대상의 다른 ③ 배정을 미룸), 이미 다시 만들었으면 `gen_hidden`(약함). 다른 사유·사유 없음 → `gen_hidden`
   (2026-11-07 스모크: "실패 기록 있음"을 "성공 기록 없음"보다 먼저 확인해 의도한 사유 대신 `record_missing`). ③' 지킨 요구의 케이스에서 실패 → `gen_hidden`.
6. ④ 허용 구현: 허용 구현이 둘 이상이고 모두 같은 케이스에서 실패 → `gen_hidden`(너무 엄격). 일부만 실패, 또는 허용 구현이 하나뿐이면
   그 구현을 먼저 (`gen_accepted`) — 이미 다시 만들었는데 또 실패하면: 흔한 구현이 그 케이스에서 의도한 사유로 실패하면(테스트는 제 몫) 설계를 못 따른 것(사람 확인),
   아니고 허용 구현이 하나뿐이면 `gen_hidden`, 그 밖은 설계 조건 부족(배정 못 함).
⑤ 공개 테스트 누설·⑧ 반복은 이 단계 밖 (공개 테스트 [미정] §4.3, 반복은 §7.1 2단계).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from task_spec.schemas import HiddenPlan, TargetRun, TestResult, VerifyReport
from task_spec.schemas.build import GeneratorName
from task_spec.starter.static_check import Overlay

MAX_MESSAGE = 220
STARTER = "starter"
COMPILE_SUFFIX = "#compile"  # regen_counts 에서 컴파일 수리 횟수 키 (일반 수리와 따로)
# 컴파일 오류 수리 요청 끝에 붙인다. 2026-11-07 알파 4회차: 수리마다 지적된 파일만 고쳐, 다음 빌드에서 다른 케이스 파일의 같은 종류
# 오류(람다 안 지역 변수·검사 예외 미선언)가 이어서 드러나 컴파일 예산 2회를 다 썼다 — 컴파일러가 한 번에 일부 파일의 오류만 보여 줄 수 있다.
SAME_KIND_ALL_FILES = (
    "\n위 파일만이 아니라 **모든 파일**에서 같은 종류의 실수(예: 람다 안에서 바뀌는 지역 변수, 검사 예외 선언 누락, "
    "없는 이름·형식)를 찾아 함께 고친다 — 컴파일러는 한 번에 일부 파일의 오류만 보여 줄 수 있다."
)


@dataclass(frozen=True)
class RepairAction:
    generator: GeneratorName
    label: str  # 오버레이 라벨 — 시작 코드·숨은 테스트는 "", 참조 구현은 naive-all · d1 …
    gate: str  # ① ② ③ ③' ④ ⑤ ⑨ setup
    reason: str  # 짧은 사유 (RepairRecord.reason, 승인 화면)
    note: str  # 생성기에 붙일 수정 요청
    compile: bool = (
        False  # 컴파일 오류 — 고치기 싸서 예산을 더 준다 (nodes/starter/build.py COMPILE_BUDGET)
    )

    @property
    def key(self) -> str:
        return f"{self.generator}:{self.label}" if self.label else self.generator

    @property
    def budget_key(self) -> str:
        """재생성 예산을 세는 키 — 컴파일 수리는 따로 센다. 2026-11-07 배송 감사 재실행: 숨은 테스트 컴파일 수리가
        일반 재생성 예산까지 써서, 컴파일이 고쳐진 뒤 드러난 실패를 고칠 기회가 없었다."""
        return f"{self.key}{COMPILE_SUFFIX}" if self.compile else self.key


@dataclass
class RepairPlan:
    actions: list[RepairAction] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)  # 배정 못 한 실패 → failed + review_flags
    flags: list[str] = field(
        default_factory=list
    )  # 막지 않는 표시 (승인자에게) — 예: 같은 사유·문구의 단언

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
                compile=prev.compile and a.compile,
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
                compile=run.setup == "compile_error",
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


def _starter_solves(
    run: TargetRun, plan: HiddenPlan, regen_counts: Mapping[str, int]
) -> tuple[list[RepairAction], list[str]]:
    """시작 코드 그대로(지원자가 아무것도 안 함)로 숨은 케이스를 **하나라도** 통과 → 새 기능이 시작 코드에 들어 있다 → `gen_starter`.
    이미 다시 만든 시작 코드가 또 통과하면 막지 않고 표시만 — 그 케이스가 지원자의 작업을 재지 않을 수 있다 (사람 확인).

    2026-11-07 알파 테스트: 2회차(새 공고)는 시작 코드가 상태 변경·감사 기록·조회를 다 구현해 hc1 을 통과, 4회차(배송 감사)는
    감사 기록 저장·조회를 구현해 hc1 만 통과(hc2 실패) — '모두 통과' 기준이 놓쳤다. 준비 실패(숨은 테스트 컴파일 오류 등)는 참조 구현 쪽에서 본다.
    """
    if run.setup != "ok":
        return [], []
    passed = [
        c.case_id
        for c in plan.cases
        if (r := run.case(c.case_id)) is not None and r.result == "PASS"
    ]
    if not passed:
        return [], []
    ids = ", ".join(passed)
    if regen_counts.get("gen_starter", 0) >= 1:
        return [], [
            f"시작 코드를 다시 만든 뒤에도 시작 코드 그대로 숨은 케이스 {ids} 를 통과 — 그 케이스가 지원자의 작업을 재지 않을 수 있다 "
            "(시작 코드 범위·숨은 테스트 사람 확인)"
        ]
    return [
        RepairAction(
            "gen_starter",
            "",
            "②'",
            f"시작 코드만으로 숨은 케이스 {ids} 통과 — 새 기능이 이미 들어 있음",
            f"시작 코드 그대로(지원자가 아무것도 하지 않은 상태)로 숨은 케이스 {ids} 를 통과했다 — 지원자가 만들 새 기능이 "
            "시작 코드에 들어 있다. '이미 있는 API' 는 공개 요구 전의 동작만 남기고(공개 요구가 더하는 기록·조회·검사는 빼고), "
            "공개 요구가 새로 요구하는 API·저장·기록은 자리 표시(TODO)로 둔다. 기존 기능 테스트는 그 동작만 확인한다.",
        )
    ], []


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
                    + text
                    + SAME_KIND_ALL_FILES,
                    compile=True,
                )
            )
        else:
            out.append(
                RepairAction(
                    _gen_for(run),
                    run.target,
                    "setup",
                    f"{run.target} 컴파일 오류",
                    "구현이 컴파일되지 않는다:\n" + text + SAME_KIND_ALL_FILES,
                    compile=True,
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
                compile=True,
            )
        )
    return out


def _signature(message: str) -> str:
    """같은 오류인지 — 숫자(id·시각)를 지우고 공백을 줄인 앞부분."""
    return " ".join(re.sub(r"\d+", "#", message).split())[:200]


def _broke_existing(run: TargetRun, starter: TargetRun) -> list[TestResult]:
    passed = {t.name for t in starter.results("existing") if t.result == "PASS"}
    return [t for t in run.results("existing") if t.result != "PASS" and t.name in passed]


def _contract_ambiguity(
    runs: Sequence[TargetRun], starter: TargetRun
) -> tuple[list[str], set[str]]:
    """참조 구현이 둘 이상이고 **모두** 시작 코드에서 통과하던 같은 기존 테스트를 같은 오류로 깨면 계약 모호 의심 (§7).

    2026-11-07 수리 루프: 명세는 "요청마다 기록(… 이전 상태 …)"인데 계약이 이전 상태를 필수로 둬, 흔한 구현·허용 구현 모두
    없는 배송 건(404) 요청을 기록하다 같은 not-null 오류로 '없음' 테스트를 깼다 — 구현을 다시 만들어도 확실히 고칠 수 없다.
    다시 만들지 않으므로 구현이 스스로 고칠 기회(그 루프의 d1)는 잃는다 — 비용·시간을 아끼는 쪽을 택함.
    """
    if len(runs) < 2:
        return [], set()
    per_run = [{t.name: _signature(t.message) for t in _broke_existing(r, starter)} for r in runs]
    common = set(per_run[0])
    for broke in per_run[1:]:
        common &= {name for name, sig in broke.items() if sig == per_run[0].get(name)}
    flags = [
        f"계약 모호 의심 — 참조 구현 {len(runs)}개가 모두 기존 테스트 {name} 를 같은 오류로 깸: {_short(next(t.message for t in runs[0].tests if t.name == name and t.group == 'existing').rsplit('Exception: ', 1)[-1])} "
        "— 계약에서 그 경우(필드 값·기록 여부)를 정해야 한다 (구현을 다시 만들지 않음)"
        for name in sorted(common)
    ]
    return flags, common


def _ref_existing(
    run: TargetRun, starter: TargetRun, skip: set[str] = frozenset()
) -> list[RepairAction]:  # type: ignore[assignment]
    broke = [t for t in _broke_existing(run, starter) if t.name not in skip]
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


def _gate3(
    run: TargetRun, plan: HiddenPlan, missed: set[str], regen_counts: Mapping[str, int]
) -> list[RepairAction]:
    passed = [
        c
        for c in plan.cases
        if c.trap_id in missed and (r := run.case(c.case_id)) is not None and r.result == "PASS"
    ]
    if passed and regen_counts.get(f"gen_naive:{run.target}", 0) == 0:
        # 흔한 구현이 숨은 케이스를 통과 — 테스트가 약한지보다 흔한 구현이 설명을 안 따랐는지를 먼저 본다.
        # 2026-11-07 배송 감사 재실행: 흔한 구현이 REQUIRES_NEW 를 상태 변경 트랜잭션이 끝난 뒤 불러 커넥션을 하나씩만 썼다.
        # 숨은 테스트는 맞았다 (진짜 흔한 구현으로 바꾸자 hc1·hc2 모두 의도한 사유로 실패). 이 회차에는 이 대상의 다른 ③ 배정을 미룬다 —
        # 흔한 구현이 틀렸으면 사유 불일치도 그 탓일 수 있다 (같은 재실행의 hc2 record_missing).
        ids = ", ".join(c.case_id for c in passed)
        traps = ", ".join(sorted({c.trap_id for c in passed}))
        return [
            RepairAction(
                "gen_naive",
                run.target,
                "③",
                f"흔한 구현이 {ids} 통과 — 흔한 구현을 먼저 다시 ({run.target})",
                f"이 흔한 구현은 숨은 요구 {traps} 를 어기는 구현이어야 하는데 숨은 케이스 {ids} 를 통과했다 — 입력의 흔한 구현 설명을 "
                "구조 그대로 따르지 않았다. 설명이 말하는 그 구조(무엇을 어디서 열고, 어느 범위 안에서 부르는지)를 그대로 쓰고, "
                "더 안전한 방식으로 바꾸지 않는다.",
            )
        ]
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
    runs: Sequence[TargetRun],
    plan: HiddenPlan,
    regen_counts: Mapping[str, int],
    naive: Sequence[TargetRun] = (),
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
            elif any(
                (n_res := n.case(c.case_id)) is not None
                and n_res.result == "ASSERT"
                and _signature(n_res.message) == _signature(res.message)
                for n in naive
            ):
                # 흔한 구현과 같은 단언·같은 메시지로 실패 — "허용 구현도 같은 함정에 빠짐"과 "테스트·계약이 둘 다를 막음"을 가를 수 없다.
                # 2026-11-07 루프: 계약에 상태 전이 규칙이 없어 숨은 테스트가 무효한 전이를 골랐고, 사유 표시까지 잘못 붙어 흔한 구현이
                # '의도한 사유'로 실패한 것처럼 보였다 → 허용 구현 탓의 근거로 쓰지 않는다.
                unresolved.append(
                    f"허용 설계 {r.target} 가 다시 만든 뒤에도 {c.case_id} 에서 흔한 구현과 같은 단언·같은 메시지로 실패: {_short(res.message)} "
                    "— 숨은 테스트·계약 의심 (사유 표시가 단언과 맞는지, 계약에 결과를 정하는 규칙이 있는지 사람 확인)"
                )
            elif any(
                (n_res := n.case(c.case_id)) is not None
                and n_res.result == "ASSERT"
                and n_res.reason == c.intended_reason
                for n in naive
            ):
                # 숨은 테스트는 흔한 구현을 의도한 사유로 잡는다 = 테스트는 제 몫을 한다 → 허용 구현이 설계를 못 따른 것.
                # 2026-11-07 수리 루프: 여기서 숨은 테스트를 의심해 다시 만들었더니 맞던 테스트가 컴파일조차 안 됐다.
                unresolved.append(
                    f"허용 설계 {r.target} 가 다시 만든 뒤에도 {c.case_id} 실패 [{res.reason or '-'}] — 숨은 테스트는 흔한 구현을 "
                    f"의도한 사유({c.intended_reason})로 잡으므로 허용 구현이 설계를 못 따름: 설계·스택 조건 보강 (사람 확인)"
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
    bare = next((r for r in report.runs if r.kind == "starter_hidden"), None)
    if bare is not None:
        acts, flags = _starter_solves(bare, plan, counts)
        res.flags += flags
        if acts:  # 참조 구현은 새 시작 코드 위에 처음부터 다시 쓰이므로 이번 결과는 보지 않는다
            res.actions = acts
            return res

    actions: list[RepairAction] = []
    ready: list[TargetRun] = []
    for run in report.runs:
        if run.kind in ("starter", "starter_hidden"):
            continue
        if run.setup != "ok":
            actions += _ref_setup(run, overlays)
            continue
        ready.append(run)
    ambiguous, skip = _contract_ambiguity(ready, starter)
    res.unresolved += ambiguous
    for run in ready:
        actions += _ref_existing(run, starter, skip)
        actions += _hidden_broken(run, plan)
    for run in ready:
        if run.kind == "ref_naive":
            actions += _gate3(run, plan, missed_traps.get(run.target, set()), counts)
    accepted = [r for r in ready if r.kind == "ref_accepted"]
    if accepted:
        acts, unresolved = _gate4(
            accepted, plan, counts, [r for r in ready if r.kind == "ref_naive"]
        )
        actions += acts
        res.unresolved += unresolved
    res.actions = _merge(actions)
    return res
