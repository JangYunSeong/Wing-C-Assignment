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
4. 숨은 케이스 `HARNESS_ERR`(테스트 자체 예외)·결과 없음 — 구현끼리 비교: 모든 구현에서면 `gen_hidden`, 일부 구현에서만이면 그 구현
   (2026-11-08: 한 구현만 계약과 다른 응답 모양을 돌려 테스트가 파싱하다 예외인데 숨은 테스트를 고치러 갔다).
   ③·④ 도 같은 생각 — 흔한 구현이 통과하면 흔한 구현부터, 허용 구현이 흔한 구현과 같은 메시지로 실패하면 허용 구현 탓으로 보지 않음.
5. ③ 흔한 구현: 놓친 숨은 요구의 케이스는 의도한 사유로 실패해야 한다. 통과 → 흔한 구현을 ③ 으로 아직 다시 만들지 않았으면(다른 게이트의 재생성과 따로 셈) `gen_naive`
   (설명을 안 따랐을 수 있음 — 그 회차에는 그 대상의 다른 ③ 배정을 미룸), 이미 다시 만들었으면 `gen_hidden`(약함). 다른 사유·사유 없음 → `gen_hidden`
   (2026-11-07 스모크: "실패 기록 있음"을 "성공 기록 없음"보다 먼저 확인해 의도한 사유 대신 `record_missing`). ③' 지킨 요구의 케이스에서 실패 → `gen_hidden`.
6. ④ 허용 구현: 허용 구현이 둘 이상이고 모두 같은 케이스에서 실패 → `gen_hidden`(너무 엄격). 일부만 실패, 또는 허용 구현이 하나뿐이면
   그 구현을 먼저 (`gen_accepted`) — 이미 다시 만들었는데 또 실패하면: 흔한 구현이 그 케이스에서 의도한 사유로 실패하면(테스트는 제 몫) 설계를 못 따른 것(사람 확인),
   아니고 허용 구현이 하나뿐이면 `gen_hidden`, 그 밖은 설계 조건 부족(배정 못 함).
⑤ 공개 테스트 누설·⑧ 반복은 이 단계 밖 (공개 테스트 [미정] §4.3, 반복은 §7.1 2단계).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace

from task_spec.schemas import HiddenPlan, TargetRun, TestResult, VerifyReport
from task_spec.schemas.build import GeneratorName
from task_spec.schemas.template import RepairTerms
from task_spec.starter.static_check import Overlay

MAX_MESSAGE = 220
STARTER = "starter"
COMPILE_SUFFIX = "#compile"  # regen_counts 에서 컴파일 수리 횟수 키 (일반 수리와 따로)
# 흔한 구현이 숨은 케이스를 통과해 다시 만든 횟수 키 (③) — 다른 게이트의 재생성과 따로 센다.
# 2026-11-09 알파 (2df170e2): 2회차에 흔한 구현을 기존 테스트 깨짐(⑤)으로 다시 만들어 예산을 쓰자, 3회차에 다시 만든 흔한 구현이
# 설명의 구조(상태 변경 트랜잭션 안에서 별도 트랜잭션 기록)를 버리고 hc1·hc2 를 통과했는데 ③ 이 흔한 구현 대신 숨은 테스트를 배정했다.
TRAP_SUFFIX = "#trap"
# 구현 실행 오류(repair_terms.impl_crash)로 다시 만든 횟수 키 — 컴파일 오류처럼 함정과 무관한 결함이라 일반 예산과 따로 센다.
# 2026-11-09 데이터 알파 6회차: 실행 오류 수리가 흔한 구현의 예산을 써, 다음 회차에 흔한 구현이 hc4 를 통과했는데(③) 다시 만들 수 없어 빌드가 끝났다.
CRASH_SUFFIX = "#crash"
# 다른 사유로 흔한 구현을 다시 만들 때 붙인다 — 지적된 실패를 고치며 설명의 구조까지 '더 안전하게' 바꾸면 함정이 사라진다 (같은 알파)
KEEP_NAIVE = (
    "\n지적된 실패만 고친다 — 입력의 흔한 구현 설명이 말하는 구조(무엇을 어디서 열고, 어느 범위 안에서 부르는지)는 "
    "그대로 둔다. 더 안전한 방식으로 바꾸지 않는다."
)
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
    budget: str = ""  # 예산을 따로 세는 키 접미사 (TRAP_SUFFIX)

    @property
    def key(self) -> str:
        return f"{self.generator}:{self.label}" if self.label else self.generator

    @property
    def budget_key(self) -> str:
        """재생성 예산을 세는 키 — 컴파일 수리는 따로 센다. 2026-11-07 배송 감사 재실행: 숨은 테스트 컴파일 수리가
        일반 재생성 예산까지 써서, 컴파일이 고쳐진 뒤 드러난 실패를 고칠 기회가 없었다."""
        return f"{self.key}{COMPILE_SUFFIX}" if self.compile else f"{self.key}{self.budget}"


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
    """컴파일 오류가 난 파일의 주인 — 숨은 테스트 파일이면 hidden, 공개 테스트 파일이면 public, 아니면 그 대상(참조 구현)."""
    for key in ("hidden", "public"):
        o = overlays.get(key)
        if o is not None and any(f.path == path for f in o.files):
            return key
    return run.target


def _shared_note(
    runs: Sequence[TargetRun], overlays: Mapping[str, Overlay], shared: Collection[str]
) -> str:
    """구현 둘 이상이 **모두** 다시 쓴 공유 파일(템플릿·시작 코드에 이미 있는 파일) — 모든 구현이 같은 오류를 낼 때 원인 후보.
    2026-11-08 알파: 참조 구현 4개가 모두 application.yml 을 다시 써 같은 404·기동 실패를 냈는데 '테스트 의심'·'계약 모호'로 짚었다."""
    if len(runs) < 2 or not shared:
        return ""
    common = set(shared)
    for r in runs:
        o = overlays.get(r.target)
        common &= {f.path for f in o.files} if o is not None else set()
    if not common:
        return ""
    return (
        f" (구현 {len(runs)}개가 모두 공유 파일 {', '.join(sorted(common))} 을 다시 씀 — 원인 후보)"
    )


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
                budget=prev.budget or a.budget,
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
                f"시작 코드 빌드 실패 ({run.setup})"
                + (f": {_short(run.setup_detail)}" if run.setup_detail else ""),
                "시작 코드만으로 빌드·기동이 되지 않는다. 이 오류를 고친다:\n"
                + (errs or _boot_text(run)),
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


PERSISTENCE = (
    "PersistenceCheckTest"  # 코드가 쓰는 저장 확인 (hidden_helpers.render_persistence_check)
)
_SERVER_SIDE = re.compile(
    r"ServletException|JpaSystemException|PersistenceException|Hibernate|DataIntegrity|com\.example\.service\.(?!grading|publictests)|status 500|<500>"
)


def _starter_public(
    run: TargetRun, regen_counts: Mapping[str, int]
) -> tuple[list[RepairAction], list[str]]:
    """시작 코드 + 공개 테스트 — 저장 확인은 통과해야 하고, 새 기능 공개 테스트는 실패해야 한다 (지원자 몫).
    2026-11-08 알파 4케이스: 시작 코드의 새 엔티티에 id 생성 전략이 없어 참조 구현 둘이 같은 오류로 깨졌다 — 저장 확인이 여기서 잡는다."""
    tests = run.results("public")
    if not tests:
        return [], []
    acts: list[RepairAction] = []
    flags: list[str] = []
    check = next((t for t in tests if t.name == PERSISTENCE), None)
    if check is not None and check.result != "PASS":
        acts.append(
            RepairAction(
                "gen_starter",
                "",
                "②",
                f"시작 코드의 새 엔티티를 저장할 수 없음: {_short(check.message)}",
                "새로 만드는 엔티티는 앱(지원자 구현)이 행을 만든다 — 저장하면 id 가 생기게 엔티티 매핑을 고친다 "
                f"(스키마의 id 열과 맞는 생성 전략). 저장 확인 실패: {_short(check.message)}",
            )
        )
    features = [t for t in tests if t.name != PERSISTENCE]
    passed = [t.name for t in features if t.result == "PASS"]
    if features and len(passed) == len(features):
        if regen_counts.get("gen_starter", 0) == 0:
            acts.append(
                RepairAction(
                    "gen_starter",
                    "",
                    "②'",
                    "시작 코드만으로 새 기능 공개 테스트를 모두 통과 — 새 기능이 이미 들어 있음",
                    f"시작 코드에서 새 기능 공개 테스트({', '.join(passed)})가 모두 통과했다 — 공개 요구가 새로 요구하는 동작은 "
                    "자리 표시(TODO)로 둔다. 이미 있는 API 는 공개 요구 전의 동작만.",
                )
            )
        else:
            flags.append(
                f"다시 만든 시작 코드에서도 새 기능 공개 테스트가 모두 통과: {', '.join(passed)} (사람 확인)"
            )
    elif passed:
        flags.append(
            f"시작 코드에서 통과하는 공개 테스트: {', '.join(passed)} — 기존 동작을 확인하는 테스트이거나 새 기능이 일부 들어 있음 (사람 확인)"
        )
    return acts, flags


def _public_refs(
    runs: Sequence[TargetRun],
    regen_counts: Mapping[str, int],
    shared: str = "",
    terms: RepairTerms | None = None,
) -> tuple[list[RepairAction], set[str]]:
    """참조 구현 + 공개 테스트 — 공개 요구는 흔한 구현·허용 구현 모두 지켜야 한다. 구현끼리 비교해 탓을 정한다.
    - 일부 구현만 실패 → 그 구현. 단 흔한 구현만 실패하고 이미 다시 만들었으면 공개 테스트가 숨은 요구를 시험하는 것(누설, ⑤) → gen_public
    - 모든 구현이 같은 오류: 서버 쪽 예외면 공유 코드(시작 코드) → gen_starter, 단언 불일치면 테스트 의심 → gen_public
    - 모든 구현이 서로 다른 오류 → gen_public
    - 구현 실행 오류(manifest repair_terms.impl_crash — 명령이 예외로 끝남): 모든 구현이 같은 오류가 아니면 그 구현마다 한 번 다시 만든다
      (실행이 안 된 구현과는 비교할 수 없어 이번엔 나머지 구현의 실패를 짚지 않는다). 다시 만든 뒤에도 실행 오류면 위 규칙대로
    돌려주는 집합 = 공개 테스트를 모두 통과한 구현 라벨 (숨은 게이트는 이 구현만 믿는다)."""
    names = sorted({t.name for r in runs for t in r.results("public")})
    trusted = {r.target for r in runs if all(t.result == "PASS" for t in r.results("public"))}
    out: list[RepairAction] = []
    for name in names:
        res = [(r, next((t for t in r.results("public") if t.name == name), None)) for r in runs]
        bad = [(r, t) for r, t in res if t is None or t.result != "PASS"]
        if not bad:
            continue
        msgs = {r.target: (t.message if t is not None else "결과 없음") for r, t in bad}
        crash_rx = re.compile(terms.impl_crash) if terms is not None and terms.impl_crash else None
        # 이미 다시 만든 구현이 또 실행 오류면 구현 탓으로 보지 않는다 — 테스트가 준 입력(경로 등)이 원인일 수 있어 아래 규칙으로
        # (2026-11-09 데이터 알파 5회차: 공개 테스트가 경로를 문자열 그대로 넘겨 모든 구현이 '입력 폴더 없음' — 구현만 두 번 다시 만들었다)
        crashed = [
            r
            for r, t in bad
            if crash_rx is not None
            and t is not None
            and t.result == "ASSERT"
            and crash_rx.search(t.message)
            and regen_counts.get(f"{_gen_for(r)}:{r.target}{CRASH_SUFFIX}", 0) < 1
        ]
        same_everywhere = len(bad) == len(runs) and len({_signature(m) for m in msgs.values()}) == 1
        if crashed and not same_everywhere:
            for r in crashed:
                out.append(
                    RepairAction(
                        _gen_for(r),
                        r.target,
                        "⑤",
                        f"{r.target} 가 공개 테스트 {name} 에서 실행 오류",
                        f"공개 테스트 {name} 에서 이 구현({r.target})의 명령이 오류로 끝났다: {_short(msgs[r.target])} — "
                        "오류가 난 코드(질의 문법·형 변환·입력 읽기)를 고친다. 입력 파일의 필드 형식은 계약 엔티티 그대로 다룬다.",
                        budget=CRASH_SUFFIX,
                    )
                )
            continue
        if len(bad) < len(runs):
            for r, _ in bad:
                if r.kind == "ref_naive" and regen_counts.get(f"gen_naive:{r.target}", 0) >= 1:
                    out.append(
                        RepairAction(
                            "gen_public",
                            "",
                            "⑤",
                            f"흔한 구현만 공개 테스트 {name} 에서 계속 실패 — 누설 의심",
                            f"흔한 구현({r.target})이 다시 만든 뒤에도 공개 테스트 {name} 에서만 실패한다: {_short(msgs[r.target])} — "
                            "공개 테스트가 숨은 요구(동시성·저장 실패 처리 등)를 시험하고 있을 수 있다. 공개 수용 기준이 말하는 응답·기록만 확인한다.",
                        )
                    )
                    continue
                out.append(
                    RepairAction(
                        _gen_for(r),
                        r.target,
                        "⑤",
                        f"{r.target} 가 공개 테스트 {name} 실패",
                        f"공개 테스트 {name} 가 이 구현({r.target})에서만 실패한다 (다른 구현은 통과): {_short(msgs[r.target])} — "
                        "공개 요구를 계약대로 지킨다.",
                    )
                )
            continue
        sigs = {_signature(m) for m in msgs.values()}
        first = next(iter(msgs.values()))
        if len(sigs) == 1 and _SERVER_SIDE.search(first):
            out.append(
                RepairAction(
                    "gen_starter",
                    "",
                    "⑤",
                    f"모든 구현이 공개 테스트 {name} 에서 같은 서버 오류 — 시작 코드(공유 코드) 의심{shared}",
                    f"참조 구현 {len(runs)}개가 모두 공개 테스트 {name} 에서 같은 서버 쪽 오류로 실패한다: {_short(first)} — "
                    "참조 구현이 함께 쓰는 시작 코드(엔티티 매핑·스키마·설정)를 고친다." + shared,
                )
            )
        else:
            out.append(
                RepairAction(
                    "gen_public",
                    "",
                    "⑤",
                    f"모든 구현이 공개 테스트 {name} 에서 실패 — 테스트 의심{shared}",
                    f"독립으로 만든 참조 구현 {len(runs)}개가 모두 공개 테스트 {name} 에서 실패한다: {_short(first)} — "
                    "계약의 응답 모양·상태 코드·규칙 그대로만 확인한다." + shared,
                )
            )
    return out, trusted


def _starter_solves(
    run: TargetRun,
    plan: HiddenPlan,
    regen_counts: Mapping[str, int],
    starter: TargetRun | None = None,
    terms: RepairTerms | None = None,
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
    missing = (
        [t.name for t in starter.results("public") if t.result != "PASS"]
        if starter is not None
        else []
    )
    if missing:
        # 시작 코드가 공개 테스트에서 실패 = 새 기능이 없다는 증거 → 기능 없이도 통과하는 숨은 케이스가 비어 있는 것.
        # 2026-11-08 알파 (2df170e2): 숨은 테스트가 계약에 없는 요청자로 요청해 모두 403 — hc2 는 "성공 기록 0건"만 단언해
        # 시작 코드 그대로 통과, 배정은 시작 코드를 두 번 다시 만들었다.
        if regen_counts.get("gen_hidden", 0) >= 1:
            return [], [
                f"숨은 테스트를 다시 만든 뒤에도 기능 없는 시작 코드(공개 테스트 {', '.join(missing)} 실패)가 숨은 케이스 {ids} 를 통과 "
                "— 그 케이스가 기능 단계에 닿지 않는다 (숨은 테스트·계약 사람 확인)"
            ]
        return [
            RepairAction(
                "gen_hidden",
                "",
                "②'",
                f"기능 없는 시작 코드가 숨은 케이스 {ids} 통과 — 숨은 테스트가 기능 단계에 닿지 않음",
                f"시작 코드 그대로(새 기능 없음 — 공개 테스트 {', '.join(missing)} 실패)로 숨은 케이스 {ids} 를 통과했다 — 이 케이스는 "
                "아무것도 구현하지 않아도 통과한다. " + (terms or RepairTerms()).feature_first,
            )
        ], []
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


def _ref_setup(
    run: TargetRun,
    overlays: Mapping[str, Overlay],
    shared: str = "",
    terms: RepairTerms | None = None,
) -> list[RepairAction]:
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
                f"{run.target} 기동 실패"
                + (f": {_short(run.setup_detail)}" if run.setup_detail else "")
                + shared,
                "구현을 넣은 앱이 기동하지 않는다 — 시작 코드의 설정·빈 구성을 깨뜨리지 않는다.\n"
                + _boot_text(run)
                + shared,
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
        if owner == "public":
            out.append(
                RepairAction(
                    "gen_public",
                    "",
                    "setup",
                    "공개 테스트 컴파일 오류",
                    f"공개 테스트가 컴파일되지 않는다. 문서와 {(terms or RepairTerms()).public_helper} 에 있는 이름·형식만 쓴다:\n"
                    + text
                    + SAME_KIND_ALL_FILES,
                    compile=True,
                )
            )
        elif owner == "hidden":
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


def _boot_text(run: TargetRun) -> str:
    """기동 실패의 근본 원인 문장 (검증기가 실패 스택의 가장 깊은 `Caused by` 를 담음) — 없으면 설정 결과만.
    2026-11-08 알파 4케이스: 'boot_error' 한 단어만 가 시작 코드가 같은 스키마 문법 오류(H2 identity 절 순서)를 되풀이했다."""
    if run.setup_detail:
        return f"근본 원인: {run.setup_detail}"
    return run.setup


def _signature(message: str) -> str:
    """같은 오류인지 — 숫자(id·시각)를 지우고 공백을 줄인 앞부분."""
    return " ".join(re.sub(r"\d+", "#", message).split())[:200]


def _broke_existing(run: TargetRun, starter: TargetRun) -> list[TestResult]:
    passed = {t.name for t in starter.results("existing") if t.result == "PASS"}
    return [t for t in run.results("existing") if t.result != "PASS" and t.name in passed]


def _contract_ambiguity(
    runs: Sequence[TargetRun], starter: TargetRun, shared: str = ""
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
        "— 계약에서 그 경우(필드 값·기록 여부)를 정해야 한다 (구현을 다시 만들지 않음)" + shared
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


def _hidden_broken(runs: Sequence[TargetRun], plan: HiddenPlan) -> list[RepairAction]:
    """숨은 케이스가 판정까지 못 감(테스트 자체 예외·결과 없음) — **구현끼리 비교해** 탓을 정한다.

    - 준비된 구현 **모두**에서 그러면 숨은 테스트 탓 → `gen_hidden`.
    - 일부 구현에서만 그러고 나머지는 판정까지 가면(통과·단언 실패) 그 구현 탓 → `gen_naive`/`gen_accepted`.
      예: 그 구현만 계약과 다른 응답 모양(목록 대신 `{content: …}` 페이지 객체)을 돌려 숨은 테스트가 파싱하다 예외.
    준비된 구현이 하나뿐이면 비교할 수 없어 숨은 테스트 탓 (예전과 같음).
    """
    out: list[RepairAction] = []
    for c in plan.cases:
        bad = [
            (r, x) for r in runs if (x := r.case(c.case_id)) is None or x.result == "HARNESS_ERR"
        ]
        if not bad:
            continue
        if len(bad) == len(runs):
            missing = [r.target for r, x in bad if x is None]
            errs = [(r.target, x.message) for r, x in bad if x is not None]
            note = (
                f"숨은 케이스 {c.case_id} 가 모든 구현에서 판정까지 가지 못했다 — "
                + (
                    "실행되지 않았다 (케이스 파일·표시 이름에 케이스 id 를 쓴다). "
                    if missing and not errs
                    else ""
                )
                + " ".join(f"{t}: 단언이 아닌 예외로 끝났다: {_short(m)}" for t, m in errs[:2])
            )
            out.append(
                RepairAction(
                    "gen_hidden",
                    "",
                    "setup",
                    f"{c.case_id} 테스트 자체 예외·결과 없음 (모든 구현)",
                    note.strip(),
                )
            )
            continue
        fine = ", ".join(r.target for r in runs if all(r is not b for b, _ in bad))
        for r, x in bad:
            what = (
                "실행되지 않았다"
                if x is None
                else f"단언이 아닌 예외로 끝났다: {_short(x.message)}"
            )
            out.append(
                RepairAction(
                    _gen_for(r),
                    r.target,
                    "setup",
                    f"{r.target} 에서만 {c.case_id} 테스트 예외",
                    f"숨은 케이스 {c.case_id} 가 이 구현({r.target})에서만 {what} — 다른 구현({fine})에서는 판정까지 갔다. "
                    "계약의 응답 모양(상태 코드·본문 필드 이름·목록/객체)과 시드 데이터를 글자 그대로 지킨다.",
                )
            )
    return out


def _gate3(
    run: TargetRun,
    plan: HiddenPlan,
    missed: set[str],
    regen_counts: Mapping[str, int],
    terms: RepairTerms | None = None,
) -> list[RepairAction]:
    passed = [
        c
        for c in plan.cases
        if c.trap_id in missed and (r := run.case(c.case_id)) is not None and r.result == "PASS"
    ]
    # 같은 숨은 요구의 다른 케이스에서 의도한 사유(계획의 어느 케이스든)로 떨어졌으면 흔한 구현은 함정을 갖고 있다 — 통과한 케이스가
    # 그 함정을 못 잰 것이니 흔한 구현을 다시 만들지 않는다. 2026-11-09 v19-loop1: 흔한 구현(따로 둔 AuditService 의 REQUIRES_NEW)이
    # hc2·hc3·hc4 를 rollback_left_success 로 떨어졌는데 hc1 만 통과해 흔한 구현을 다시 만들었고, 다시 만든 판은 REQUIRES_NEW 를
    # 같은 클래스 안에서 불러(프록시를 안 거쳐 무효) 함정을 잃고 숨은 케이스를 모두 통과했다.
    intended = {c.intended_reason for c in plan.cases}
    traps = {c.trap_id for c in passed}
    shows_trap = any(
        c.trap_id in traps
        and (r := run.case(c.case_id)) is not None
        and r.result == "ASSERT"
        and r.reason in intended
        for c in plan.cases
    )
    if (
        passed
        and not shows_trap
        and regen_counts.get(f"gen_naive:{run.target}{TRAP_SUFFIX}", 0) == 0
    ):
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
                budget=TRAP_SUFFIX,
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
                        f"흔한 구현({run.target})이 숨은 케이스 {c.case_id} 를 통과했다. 통과 조건({c.expect})은 그대로 두고 "
                        "올바른 구현이 통과하는 단언을 유지한다 — 숨은 요구 "
                        f"{c.trap_id} 를 어기는 구현이 어기게 되는 관찰 결과(기대와 다른 응답·시간 초과가 없어야 함, 요청마다 결과와 맞는 기록 등)를 "
                        f"그 조건대로 확인하고, 그 단언에 의도한 사유({c.intended_reason})를 붙인다. 실패나 시간 초과가 **일어나야 한다**고 "
                        "단언하지 않는다 — 그러면 올바른 구현이 떨어진다 (2026-11-07 알파 5회차).",
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
                        f"{_short(r.message)} — 의도한 실패를 가장 먼저 확인하는 단언 순서로, 사유는 {(terms or RepairTerms()).reason_call} 로 붙인다.",
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
    shared_paths: Collection[str] = (),
    terms: RepairTerms | None = None,
) -> RepairPlan:
    """검증 결과 → 다시 만들 생성기 목록. overlays 키는 "starter"·"hidden"·참조 구현 라벨.
    missed_traps: 흔한 구현 라벨 → 그 구현이 놓친 숨은 요구 id (naive-all 은 전부).
    shared_paths: 템플릿·시작 코드에 이미 있는 파일 — 모든 구현이 같은 오류를 낼 때, 모두 다시 쓴 파일을 원인 후보로 싣는다."""
    counts = regen_counts or {}
    res = RepairPlan()
    starter = report.run(STARTER)
    if starter is None:
        res.unresolved.append("시작 코드 검증 결과가 없음")
        return res
    if acts := _starter(starter):
        res.actions = acts
        return res
    acts, flags = _starter_public(starter, counts)
    res.flags += flags
    if (
        acts
    ):  # 시작 코드가 틀리면 참조 구현은 새 시작 코드 위에 다시 쓰이므로 이번 결과는 보지 않는다
        res.actions = acts
        return res
    bare = next((r for r in report.runs if r.kind == "starter_hidden"), None)
    if bare is not None:
        acts, flags = _starter_solves(bare, plan, counts, starter, terms)
        res.flags += flags
        if acts:  # 참조 구현은 새 시작 코드 위에 처음부터 다시 쓰이므로 이번 결과는 보지 않는다
            res.actions = acts
            return res

    actions: list[RepairAction] = []
    ready: list[TargetRun] = []
    refs = [r for r in report.runs if r.kind not in ("starter", "starter_hidden")]
    booted = [r for r in refs if r.setup == "boot_error"]
    boot_note = (
        _shared_note(booted, overlays, shared_paths)
        if len(booted) == len(refs) and len({_signature(r.setup_detail) for r in booted}) == 1
        else ""
    )
    for run in refs:
        if run.setup != "ok":
            actions += _ref_setup(run, overlays, boot_note, terms)
            continue
        ready.append(run)
    shared = _shared_note(ready, overlays, shared_paths)
    ambiguous, skip = _contract_ambiguity(ready, starter, shared)
    res.unresolved += ambiguous
    for run in ready:
        actions += _ref_existing(run, starter, skip)
    public_acts, trusted = _public_refs(ready, counts, shared, terms)
    actions += public_acts
    # 숨은 게이트는 공개 테스트를 통과한 구현만 믿는다 — 공개 요구가 깨진 구현의 숨은 결과는 그 결함 탓일 수 있다
    # (2026-11-08: 시작 코드 결함으로 두 구현이 같은 500 을 내자 흔한 구현이 '의도한 사유'로 실패한 것처럼 보였다)
    judged = [r for r in ready if r.target in trusted]
    actions += _hidden_broken(judged, plan)
    for run in judged:
        if run.kind == "ref_naive":
            actions += _gate3(run, plan, missed_traps.get(run.target, set()), counts, terms)
    accepted = [r for r in judged if r.kind == "ref_accepted"]
    if accepted:
        acts, unresolved = _gate4(
            accepted, plan, counts, [r for r in judged if r.kind == "ref_naive"]
        )
        actions += acts
        res.unresolved += unresolved
    res.actions = [
        replace(a, note=a.note + KEEP_NAIVE) if a.generator == "gen_naive" and not a.budget else a
        for a in _merge(actions)
    ]
    return res
