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
   흔한 구현이 필수 필드를 빠뜨린 결함이었다 — 참조 구현을 먼저 다시 만든다). 다시 만든 구현이 모두 또 같은 기존 테스트를 깨면(오류는 달라도)
   그 테스트가 계약에 없는 동작을 단언한 것 → `gen_starter` (2026-11-15 데이터 알파: 기존 테스트가 '미구현 예외'를 단언).
4. 숨은 케이스 `HARNESS_ERR`(테스트 자체 예외)·결과 없음 — 구현끼리 비교: 모든 구현에서면 `gen_hidden`, 일부 구현에서만이면 그 구현
   (2026-11-08: 한 구현만 계약과 다른 응답 모양을 돌려 테스트가 파싱하다 예외인데 숨은 테스트를 고치러 갔다).
   ③·④ 도 같은 생각 — 흔한 구현이 통과하면 흔한 구현부터, 허용 구현이 흔한 구현과 같은 메시지로 실패하면 허용 구현 탓으로 보지 않음.
5. ③ 흔한 구현: 놓친 숨은 요구의 케이스는 의도한 사유로 실패해야 한다 (숨은 요구마다 따로 판정 — #242). 통과 → 흔한 구현을 ③ 으로 아직 다시 만들지 않았으면(다른 게이트의 재생성과 따로 셈) `gen_naive`
   (설명을 안 따랐을 수 있음 — 그 회차에는 그 대상의 다른 ③ 배정을 미룸), 이미 다시 만들었으면 `gen_hidden`(약함). 다른 사유·사유 없음 → `gen_hidden`
   (2026-11-07 스모크: "실패 기록 있음"을 "성공 기록 없음"보다 먼저 확인해 의도한 사유 대신 `record_missing`).
   흔한 구현은 모든 숨은 요구를 놓친 판(naive-all) 하나다 — 하나만 놓친 판과 그 ③'(지킨 요구의 케이스는 통과해야 함)은 만드는 곳이
   없어 지웠다 (#243, 숨은 요구 여러 개를 지원할 때 다시 설계).
6. ④ 허용 구현: 허용 구현이 둘 이상이고 모두 같은 케이스에서 실패 → `gen_hidden`(너무 엄격). 일부만 실패, 또는 허용 구현이 하나뿐이면
   그 구현을 먼저 (`gen_accepted`) — 이미 다시 만들었는데 또 실패하면: 흔한 구현이 그 케이스에서 의도한 사유로 실패하면(테스트는 제 몫) 설계를 못 따른 것(사람 확인),
   아니고 허용 구현이 하나뿐이면 `gen_hidden`, 그 밖은 설계 조건 부족(배정 못 함).
⑤ 공개 테스트 누설·⑧ 반복은 이 단계 밖 (공개 테스트 [미정] §4.3, 반복은 §7.1 2단계).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Literal

from task_spec.schemas import (
    HiddenCase,
    HiddenPlan,
    TargetRun,
    TaskContract,
    TestResult,
    VerifyReport,
)
from task_spec.schemas.build import GeneratorName
from task_spec.schemas.template import RepairTerms
from task_spec.starter.static_check import Overlay, seed_literal_misses

MAX_MESSAGE = 220
STARTER = "starter"
# ── 재생성 예산 (#183) ───────────────────────────────────────
# 산출물(생성기·라벨)마다, 풀마다 따로 센다. 풀은 이 표 하나 — 사례마다 접미사를 하나씩 더하고 조회하는 곳마다 키를 손으로 조립하던 것을
# 정리했다 (키·'다 썼나' 판정은 budget_key·exhausted). 빌드 전체 회차 상한(MAX_ROUNDS)은 풀과 무관하다.
# 부류: general — 산출물마다 한 번 · defect — 함정과 무관한 결함(컴파일·실행 오류·다시 만든 구현의 퇴행) · suspect_test — 테스트를 한 번
# 의심 · naive_first — ③ 에서 흔한 구현을 먼저 한 번 다시 · gate — 관문(⑩·⑪) 수리.
COMPILE_RETRIES = 2  # 컴파일 오류 재요청 상한 — 수리 루프의 컴파일 풀, 검증 전 채점 코드·구현 컴파일 재요청이 같이 쓴다
PoolKind = Literal["general", "defect", "suspect_test", "naive_first", "gate"]


@dataclass(frozen=True)
class Pool:
    suffix: str  # regen_counts 키 접미사 ('' = 일반 풀)
    limit: int  # 이 풀로 다시 만들 수 있는 횟수 (산출물마다)
    kind: PoolKind


GENERAL = Pool("", 1, "general")
# 컴파일 오류 — 고치기 싸서 2회. 2026-11-07 배송 감사 재실행: 숨은 테스트 컴파일 수리가 일반 예산까지 써서, 컴파일이 고쳐진 뒤 드러난
# 실패를 고칠 기회가 없었다. 2026-11-07 루프: 1회 수리가 틀린 방법으로 고쳐 예산이 끝났다.
COMPILE = Pool("#compile", COMPILE_RETRIES, "defect")
# 구현 실행 오류(repair_terms.impl_crash). 2026-11-09 데이터 알파 6회차: 실행 오류 수리가 흔한 구현의 예산을 써, 다음 회차에 흔한 구현이
# hc4 를 통과했는데(③) 다시 만들 수 없어 빌드가 끝났다.
CRASH = Pool("#crash", 1, "defect")
# 다른 게이트 때문에 다시 만든 구현이 이전에 통과하던 숨은 케이스에서 떨어짐 — 구현 퇴행이라 구현을 한 번 더 (#183, _gate4).
# 2026-11-14 naver_2026#002: 공개 테스트(⑤)로 다시 만든 d2 가 같은 번호 동시 요청에 500 을 냈는데 '설계와 모순 의심'으로 멀쩡한 숨은
# 테스트를 두 번 다시 만들다 빌드가 끝났다.
REGRESS = Pool("#regress", 1, "defect")
# 다른 허용 구현은 통과하는 케이스에서 한 허용 구현만 다시 만든 뒤에도 떨어질 때 테스트를 한 번 의심. 2026-11-10 프론트 알파
# 49507571·008d6314: 숨은 테스트가 한 설계의 진행(이어 붙인 뒤 정확히 40건)을 단언해 다른 허용 설계를 막았는데 '허용 구현 탓'으로 끝났다.
DESIGN = Pool("#design", 1, "suspect_test")
# ③ 흔한 구현이 숨은 케이스를 통과 — 숨은 테스트보다 흔한 구현을 먼저 한 번. 2026-11-09 알파 (2df170e2): 다른 게이트(⑤) 재생성이
# 일반 예산을 써, 다시 만든 흔한 구현이 설명의 구조를 버리고 hc1·hc2 를 통과했는데 ③ 이 흔한 구현 대신 숨은 테스트를 배정했다.
NAIVE_FIRST = Pool("#naive-first", 1, "naive_first")
VARIANT = Pool("#variant", 1, "gate")  # 공개 테스트 변별 관문 ⑩ (#164) — 테스트 클래스 더하기
TRAP_GATE = Pool("#trap-gate", 1, "gate")  # 숨은 테스트 함정 관문 ⑪ (#171) — 숨은 케이스 다시
# 손 구현 관문 ⑫ (#256 3단계) — 재료 손 구현이 짚은 숨은·공개 테스트를 생성기마다 한 번 다시
HAND = Pool("#hand", 1, "gate")
# 보정 창 없음 뒤 숨은 케이스 한 번 다시 (설계 검사 #332 T-F1) — 창 유무는 재료보다 그 회차의 버스트 대상·명세 수치·생성 코드에 달려
# 같은 재료가 다른 회차에서는 보정됐다 (알파 r2-1 ↔ r2-2). 한 번 다시 해도 창이 없으면 그때 재료 판정으로 멈춘다
CALIBRATE = Pool("#calibrate", 1, "gate")
POOLS = (GENERAL, COMPILE, CRASH, REGRESS, DESIGN, NAIVE_FIRST, VARIANT, TRAP_GATE, HAND, CALIBRATE)


@dataclass(frozen=True)
class RegenNote:
    """산출물을 마지막으로 다시 만든 이유 — 게이트·겨냥한 숨은 케이스·사유 (#183: 예산 기록이 '다시 만들었다'만 남겨, '이 케이스
    때문에 다시 만들었는데도 또 떨어짐'과 '다른 이유로 다시 만든 뒤 퇴행'을 가르지 못했다)."""

    gate: str
    cases: tuple[str, ...]
    reason: str
    # 모든 구현이 떨어진 테스트 (`public:pt3` · `hidden:hc1`) — 같은 테스트로 또 오면 계약 결함 의심 (#256 4단계, _contract_suspects)
    blame: tuple[str, ...] = ()


def budget_key(generator: str, label: str = "", pool: Pool = GENERAL) -> str:
    """regen_counts 키 — `생성기[:라벨]` + 풀 접미사."""
    return (f"{generator}:{label}" if label else generator) + pool.suffix


def exhausted(
    counts: Mapping[str, int], generator: str, label: str = "", pool: Pool = GENERAL
) -> bool:
    """그 산출물의 그 풀을 다 썼나."""
    return counts.get(budget_key(generator, label, pool), 0) >= pool.limit


# 변별하지 못해 채점에서 뺀 숨은 케이스 표시의 끝말 — 손 구현 관문(⑫)이 그 케이스를 남기면 build 가 이 표시를 거둔다
WEAK_FLAG = "흔한 구현과 올바른 구현을 구별하지 않아 채점에서 제외했다"
# 모든 구현이 같은 테스트에서 떨어지는 수리의 사유 머리 (#256 4단계) — 그 테스트를 다시 만든 뒤에도 같은 꼴이면 테스트를 또 만들지 않고
# '계약 결함 의심'으로 멈춘다 (assign 끝). 첫 번은 테스트를 다시 만든다 — 테스트 결함이 더 흔한 원인이라서.
ALL_PUBLIC_FAIL = "모든 구현이 공개 테스트"
ALL_ACCEPTED_FAIL = "허용 구현 모두 "
CONTRACT_SUSPECT = "계약 결함 의심"
# 흔한 구현이 정답 장치를 쓴다는 재료 판정 — 막는 표시(unresolved)로 옮긴다 (assign)
MATERIAL_VERDICT = "재료 판정: "
# 다른 사유로 흔한 구현을 다시 만들 때 붙인다 — 지적된 실패를 고치며 설명의 구조까지 '더 안전하게' 바꾸면 함정이 사라진다 (같은 알파)
# (#182: 예전 문구의 괄호 '무엇을 어디서 열고, 어느 범위 안에서 부르는지'는 트랜잭션 사례의 모양이라 모든 스택에 나갔다 — 2026-11-09 dc2b08b)
KEEP_NAIVE = (
    "\n지적된 실패만 고친다 — 입력의 흔한 구현 설명이 말하는 구조는 그대로 둔다. 실패를 고치려고 설명에 없는 장치를 더하거나 "
    "더 안전한 방식으로 바꾸지 않는다."
)
# 컴파일 오류 수리 요청 끝에 붙인다. 2026-11-07 알파 4회차: 수리마다 지적된 파일만 고쳐, 다음 빌드에서 다른 케이스 파일의 같은 종류
# 오류(람다 안 지역 변수·검사 예외 미선언)가 이어서 드러나 컴파일 예산 2회를 다 썼다 — 컴파일러가 한 번에 일부 파일의 오류만 보여 줄 수 있다.
# (#182: 예전 괄호 예시 '람다 안에서 바뀌는 지역 변수, 검사 예외 선언 누락'은 Java 모양이라 TypeScript 컴파일 수리에도 붙었다)
SAME_KIND_ALL_FILES = (
    "\n위 파일만이 아니라 **모든 파일**에서 위 오류와 같은 원인의 실수를 찾아 함께 고친다 — 컴파일러는 한 번에 일부 파일의 "
    "오류만 보여 줄 수 있다."
)


@dataclass(frozen=True)
class RepairAction:
    generator: GeneratorName
    label: str  # 오버레이 라벨 — 시작 코드·숨은 테스트는 "", 참조 구현은 naive-all · d1 …
    gate: str  # ① ② ③ ④ ⑤ ⑨ setup
    reason: str  # 짧은 사유 (RepairRecord.reason, 승인 화면)
    note: str  # 생성기에 붙일 수정 요청
    pool: Pool = GENERAL  # 재생성 예산 풀 (POOLS)
    # 이 수리가 겨냥한 숨은 케이스 — 다시 만든 뒤 다른 케이스에서 떨어지면 퇴행으로 본다 (#183, _gate4)
    cases: tuple[str, ...] = ()
    # 모든 구현이 떨어진 테스트 (`public:pt3` · `hidden:hc1`) — 사유 글(잘리고 합쳐짐)이 아니라 이 칸으로 되풀이를 본다 (#256 4단계)
    blame: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.generator}:{self.label}" if self.label else self.generator

    @property
    def budget_key(self) -> str:
        return budget_key(self.generator, self.label, self.pool)

    @property
    def compile(self) -> bool:
        """컴파일 오류 수리인가 (컴파일 풀)."""
        return self.pool is COMPILE


@dataclass
class RepairPlan:
    actions: list[RepairAction] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)  # 배정 못 한 실패 → failed + review_flags
    flags: list[str] = field(
        default_factory=list
    )  # 막지 않는 표시 (승인자에게) — 예: 같은 사유·문구의 단언
    # 채점에서 뺄 숨은 케이스 — 다시 만든 뒤에도 흔한 구현이 통과하고 같은 숨은 요구를 다른 케이스가 의도한 사유로 잡는 것
    # (변별하지 못함) [확정: 2026-11-11 사용자 — 사람 확인 표시만 하던 것을 코드가 뺀다]
    weak: list[str] = field(default_factory=list)
    # 다시 만들기 전에 조절값부터 맞춰 볼 (흔한 구현 라벨, 숨은 케이스) — 흔한 구현이 허용 설계 장치 없이 통과했고 그 사유에 보정할
    # 숨긴 조절값이 있다 (#301 PR 2, starter/calibrate.py). 빌드가 보정을 돌리고, 못 맞추면 이 칸 없이 다시 배정한다
    calibrate: list[tuple[str, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.actions and not self.unresolved and not self.calibrate


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
    return f" (구현 {len(runs)}개가 모두 공유 파일 {', '.join(sorted(common))} 을 다시 작성함 — 원인 후보)"


def _gen_for(run: TargetRun) -> GeneratorName:
    return "gen_naive" if run.kind == "ref_naive" else "gen_accepted"


def _hidden_failures(run: TargetRun) -> list[str]:
    """그 구현이 단언에서 떨어진 숨은 케이스 (실행 오류·하네스 오류는 빼고)."""
    return [t.name for t in run.results("hidden") if t.result == "ASSERT"]


_GATE = re.compile(r"[①-⑳]'?|[a-z]+")


def gates_of(gate: str) -> list[str]:
    """수리의 게이트 — `_merge` 가 사유를 합치며 이어 붙인 게이트(예: "③④")를 나눈다. ②' 처럼 ' 가 붙은 게이트는 하나로 본다."""
    return _GATE.findall(gate)


def _merged_pool(p: Pool, q: Pool) -> Pool:
    """사유를 합친 수리의 풀 — 둘 다 컴파일이면 컴파일, 아니면 따로 세는 풀(일반·컴파일 밖)이 하나라도 있으면 그것, 아니면 일반."""
    if p is COMPILE and q is COMPILE:
        return COMPILE
    return next((x for x in (p, q) if x not in (GENERAL, COMPILE)), GENERAL)


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
                pool=_merged_pool(prev.pool, a.pool),
                cases=tuple(dict.fromkeys((*prev.cases, *a.cases))),
                blame=tuple(dict.fromkeys((*prev.blame, *a.blame))),
            )
    return list(out.values())


CONFORM_NOTE = (
    "계약 적합성 스모크(계약 모양 그대로의 응답을 바로 주고 입력란에 글자를 넣음)에서 앱이 깨졌다 — 응답 본문은 계약의 모양 "
    "그대로 읽는다 (계약 응답이 배열이면 배열, 칸 이름 그대로 — 감싼 객체로 읽지 않는다). 받은 값이 비어도 화면이 내려가지 않게 한다. 실패한 화면:"
)


def _conform(runs: Sequence[TargetRun]) -> list[RepairAction]:
    """게이트 ⓪ — 계약 적합성 스모크 (starter/conformance.py, 계약에서 코드가 쓴 검사). 탓을 실패 메시지 꼴이 아니라 증거로 정한다:
    시작 코드가 깨지면 시작 코드 · 참조 구현이 모두 같은 화면에서 깨지면 공유 코드(시작 코드) · 일부만 깨지면 그 구현.
    2026-11-10 프론트 알파(fefix e99cbba0): 시작 코드 api 모듈이 배열 응답을 `{ items }` 로 읽어 구현들이 화면째 내려갔는데, ⑤가 공개 테스트를
    의심해 다시 만들고 예산을 다 썼다."""

    def bad(r: TargetRun) -> list[TestResult]:
        return [t for t in r.results("conform") if t.result != "PASS"]

    def lines(ts: list[TestResult]) -> str:
        return "\n".join(f"- {t.name}: {_short(t.message)}" for t in ts[:6])

    starter = next((r for r in runs if r.kind == "starter"), None)
    if starter is not None and (b := bad(starter)):
        return [
            RepairAction(
                "gen_starter",
                "",
                "⓪",
                f"시작 코드가 계약 적합성 스모크 실패: {_short(b[0].name)}",
                f"{CONFORM_NOTE}\n{lines(b)}",
            )
        ]
    refs = [r for r in runs if r.kind in ("ref_naive", "ref_accepted") and r.setup == "ok"]
    failing = [(r, b) for r in refs if (b := bad(r))]
    if not failing:
        return []
    if (
        len(refs) >= 2
        and len(failing) == len(refs)
        and len({tuple(sorted(t.name for t in b)) for _, b in failing}) == 1
    ):
        return [
            RepairAction(
                "gen_starter",
                "",
                "⓪",
                f"참조 구현 모두 같은 화면에서 계약 적합성 스모크 실패 — 공유 코드 의심: {_short(failing[0][1][0].name)}",
                f"시작 코드 위의 구현 {len(refs)}개가 모두 같은 화면에서 깨진다 — 구현이 함께 쓰는 시작 코드(api 모듈·화면 뼈대)를 "
                f"계약대로 고친다. {CONFORM_NOTE}\n{lines(failing[0][1])}",
            )
        ]
    return [
        RepairAction(
            _gen_for(r),
            r.target,
            "⓪",
            f"{r.target} 가 계약 적합성 스모크 실패: {_short(b[0].name)}",
            f"{CONFORM_NOTE}\n{lines(b)}",
        )
        for r, b in failing
    ]


def _starter(
    run: TargetRun,
    overlays: Mapping[str, Overlay] | None = None,
    terms: RepairTerms | None = None,
    contract: TaskContract | None = None,
) -> list[RepairAction]:
    # 컴파일 오류가 모두 공개·숨은 테스트 파일 안이면 시작 코드 탓이 아니다 — 그 파일을 쓴 생성기로 (참조 구현과 같은 규칙).
    # 2026-11-10 프론트 알파 fe-v3-opt: 다시 만든 공개 테스트의 TS2554 를 '시작 코드 빌드 실패'로 짚어 시작 코드만 다시 만들었다.
    if (
        run.setup == "compile_error"
        and overlays
        and run.compile_errors
        and all(_owner(e.path, run, overlays) in ("public", "hidden") for e in run.compile_errors)
    ):
        return _ref_setup(run, overlays, terms=terms)
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
                pool=COMPILE if run.setup == "compile_error" else GENERAL,
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
            + lines
            + _seed_hint(bad, overlays, contract),
        )
    ]


def _seed_hint(
    bad: Sequence[TestResult],
    overlays: Mapping[str, Overlay] | None,
    contract: TaskContract | None,
) -> str:
    """실제로 떨어진 기존 테스트 파일이 저장소 조회에 넘긴 글자 값 중 시드에 없는 것 — 실제 값 예를 붙인다.
    2026-11-25 prod-b4: findByUsername("user1") 이 시드(username-<id>)에 없어 떨어졌는데, 수리 요청에 실제 값이 없어 다시 만들어도
    같은 테스트를 썼다. 정적 검사로 미리 막지 않는다 — 없는 값을 찾는 정상 테스트도 같은 꼴이다 (운영 b5, static_check.seed_literal_misses)."""
    starter = (overlays or {}).get(STARTER)
    if starter is None or contract is None:
        return ""
    failed = {t.name for t in bad}
    files = [f for f in starter.files if PurePosixPath(f.path).stem in failed]
    misses = seed_literal_misses(files, contract)
    if not misses:
        return ""
    return (
        "\n시드에 없는 값으로 조회한 곳 — 행이 있기를 기대하는 테스트라면 시드 값을 쓰고(README 시드 줄), 없음을 확인하는 테스트라면 "
        "그대로 둔다:\n"
        + "\n".join(
            f"- {PurePosixPath(m.path).name}: {m.call} — 시드의 {m.field} 실제 값 예: {m.sample}"
            for m in misses[:5]
        )
    )


PERSISTENCE = (
    "PersistenceCheckTest"  # 코드가 쓰는 저장 확인 (hidden_helpers.render_persistence_check)
)


_PT_NO = re.compile(r"^[Pp]t(\d{1,2})(?!\d)")


def _starter_public(
    run: TargetRun, regen_counts: Mapping[str, int], new_tests: Collection[str] = ()
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
        if not exhausted(regen_counts, "gen_starter"):
            acts.append(
                RepairAction(
                    "gen_starter",
                    "",
                    "②'",
                    "시작 코드만으로 새 기능 공개 테스트를 모두 통과 — 새 기능을 이미 포함함",
                    f"시작 코드에서 새 기능 공개 테스트({', '.join(passed)})가 모두 통과했다 — 공개 요구가 새로 요구하는 동작은 "
                    "자리 표시(TODO)로 둔다. 이미 있는 API 는 공개 요구 전의 동작만.",
                )
            )
        else:
            flags.append(
                f"다시 생성한 시작 코드에서도 새 기능 공개 테스트가 모두 통과: {', '.join(passed)} (사람 확인)"
            )
    elif new_hits := [
        n
        for n in passed
        if (m := _PT_NO.match(n)) is not None and f"pt{int(m.group(1))}" in new_tests
    ]:
        # 새 API 에만 걸린 공개 테스트가 시작 코드로 통과 — 이미 있는 동작일 수 없다. 2026-11-15 손 시범 coupons: 계약이 새 차감 API 를
        # existing 으로 적어 시작 코드가 차감·거절을 구현했고, 공개 테스트 4개 중 1개만 통과해 '모두 통과' 기준을 비켜 표시로만 남았다
        if not exhausted(regen_counts, "gen_starter"):
            acts.append(
                RepairAction(
                    "gen_starter",
                    "",
                    "②'",
                    f"시작 코드만으로 새 API 의 공개 테스트 통과 ({', '.join(new_hits)}) — 새 기능을 포함함",
                    f"시작 코드에서 새 API(계약의 existing 이 아닌 엔드포인트)의 공개 테스트({', '.join(new_hits)})가 통과했다 — 새 API 의 "
                    "처리는 자리 표시(TODO)로 둔다. 이미 있는 API 만 공개 요구 전의 동작으로 구현한다.",
                )
            )
        else:
            flags.append(
                f"다시 생성한 시작 코드에서도 새 API 의 공개 테스트가 통과: {', '.join(new_hits)} (사람 확인)"
            )
    elif passed:
        flags.append(
            f"시작 코드에서 통과하는 공개 테스트: {', '.join(passed)} — 기존 동작을 확인하는 테스트이거나 새 기능을 일부 포함함 (사람 확인)"
        )
    return acts, flags


def _public_refs(
    runs: Sequence[TargetRun],
    regen_counts: Mapping[str, int],
    shared: str = "",
    terms: RepairTerms | None = None,
) -> tuple[list[RepairAction], set[str], list[str]]:
    """참조 구현 + 공개 테스트 — 공개 요구는 흔한 구현·허용 구현 모두 지켜야 한다. 구현끼리 비교해 탓을 정한다.
    - 일부 구현만 실패 → 그 구현. 단 흔한 구현만 실패하고 이미 다시 만들었으면 공개 테스트가 숨은 요구를 시험하는 것(누설, ⑤) → gen_public
    - 모든 구현이 같은 오류: 서버 쪽 예외면 공유 코드(시작 코드) → gen_starter, 단언 불일치면 테스트 의심 → gen_public
    - 모든 구현이 서로 다른 오류 → gen_public
    - 구현 실행 오류(manifest repair_terms.impl_crash — 명령이 예외로 끝남): 모든 구현이 같은 오류가 아니면 그 구현마다 한 번 다시 만든다
      (실행이 안 된 구현과는 비교할 수 없어 이번엔 나머지 구현의 실패를 짚지 않는다). 다시 만든 뒤에도 실행 오류면 위 규칙대로
    돌려주는 값 = (수리, 공개 테스트를 모두 통과한 구현 라벨 — 숨은 게이트는 이 구현만 믿는다, 배정하지 않고 사람에게 넘길 실패)."""
    names = sorted({t.name for r in runs for t in r.results("public")})
    trusted = {r.target for r in runs if all(t.result == "PASS" for t in r.results("public"))}
    server = re.compile(terms.server_side) if terms is not None and terms.server_side else None
    out: list[RepairAction] = []
    # 테스트를 의심하지 않고 멈출 구현 → (실행, 틀린 공개 테스트)
    held: dict[str, tuple[TargetRun, list[str]]] = {}
    for name in names:
        res = [(r, next((t for t in r.results("public") if t.name == name), None)) for r in runs]
        bad = [(r, t) for r, t in res if t is None or t.result != "PASS"]
        if not bad:
            continue
        msgs = {r.target: (t.message if t is not None else "결과 없음") for r, t in bad}
        defect = _test_defect(msgs, terms)
        # 실패한 단언의 줄·코드를 수리 요청에 (2026-11-13 N+1 알파: 메시지만으로는 같은 틀린 단언을 되풀이했다) — 판정(_signature)에는 쓰지 않는다
        lines = {r.target: t.at for r, t in bad if t is not None and t.at}
        if defect:
            out.append(
                RepairAction(
                    "gen_public",
                    "",
                    "⑤",
                    f"공개 테스트 {name} 가 하네스가 지적한 테스트 결함으로 실패 ({', '.join(defect)})",
                    f"하네스가 공개 테스트 {name} 의 결함을 짚었다: {_short(msgs[defect[0]])} — 구현 탓이 아니다. 테스트를 고친다.",
                )
            )
            continue
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
            and not exhausted(regen_counts, _gen_for(r), r.target, CRASH)
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
                        pool=CRASH,
                    )
                )
            continue
        if len(bad) < len(runs):
            for r, _ in bad:
                if r.kind == "ref_naive" and exhausted(regen_counts, "gen_naive", r.target):
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
                if (
                    r.kind == "ref_accepted"
                    and exhausted(regen_counts, "gen_accepted", r.target)
                    and _hidden_failures(r)
                ):
                    # 다시 만든 허용 구현이 이 공개 테스트와 숨은 케이스를 함께 틀렸다 — 따로 만든 두 테스트 묶음이 함께 틀렸기보다 구현 하나가
                    # 틀렸을 가능성이 커 테스트를 의심하지 않는다. 2026-11-25 재시도 중복 알파(fix-retry): d2 가 @Transactional 메서드를 같은
                    # 클래스 안에서 불러 잔액·결과가 저장되지 않아 pt1·hc1·hc2 를 함께 틀렸는데, 아래 갈래가 맞던 pt1 을 다시 만들어
                    # '잔액이 실제로 늘었나' 단언을 잃었다. 과거 테스트 의심 수리(11-11 fault3 등 5건)는 그 회차에 한쪽 묶음만 틀렸다.
                    held.setdefault(r.target, (r, []))[1].append(name)
                    continue
                if (
                    r.kind == "ref_accepted"
                    and exhausted(regen_counts, "gen_accepted", r.target)
                    and not exhausted(regen_counts, "gen_public", pool=DESIGN)
                ):
                    # 다시 만든 허용 구현만 또 떨어지고 다른 구현은 통과 — 공개 테스트가 한 구현 방식에 기댔을 수 있다 (게이트 ④ 와 같은 규칙).
                    # 2026-11-10 응답 순서 cf4: 공개 pt1 이 '서버'를 치고 조건 없이 먼저 온 요청('서')에 응답해, 지난 응답을 무시하는 허용 구현 d2 만
                    # 목록을 못 보여 줬다 — d2 를 두 번 다시 만들고 예산을 다 썼다. 공개 테스트 입력에는 설계를 넣지 않는다 (숨은 요구의 해법).
                    passing = [x.target for x, t in res if t is not None and t.result == "PASS"]
                    out.append(
                        RepairAction(
                            "gen_public",
                            "",
                            "⑤",
                            f"다시 생성한 {r.target} 만 공개 테스트 {name} 실패 — 테스트가 한 구현 방식에 의존함 의심",
                            f"공개 테스트 {name} 가 다시 만든 구현({r.target})에서만 또 실패하고 다른 구현({', '.join(passing)})은 통과한다: "
                            f"{_short(msgs[r.target])} — 올바른 구현마다 다를 수 있는 것(처리 순서·횟수·중간 상태)에 기대지 않는지 본다. "
                            "확인할 대상이 여럿일 수 있으면 공개 수용 기준이 말하는 그것을 골라 확인하고, 그 결과만 단언한다.",
                            pool=DESIGN,
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
        # 서버 쪽(공유 코드) 예외의 꼴은 매니페스트 repair_terms.server_side (#176 — Spring 예외 이름이 공용 수리 코드에 있었다)
        if len(sigs) == 1 and server is not None and server.search(first):
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
                    f"{ALL_PUBLIC_FAIL} {name} 에서 실패 — 테스트 의심{shared}",
                    f"독립으로 만든 참조 구현 {len(runs)}개가 모두 공개 테스트 {name} 에서 실패한다: {_short(first)}"
                    + (f" — 실패한 줄: {'; '.join(sorted(set(lines.values())))}" if lines else "")
                    + " — 그 단언이 계약·시드의 실제 데이터에서 성립하는지(조회가 그 행을 돌려줄 수 있는지, 값이 그 값인지) 보고 "
                    "계약의 응답 모양·상태 코드·규칙 그대로만 확인한다." + shared,
                    blame=(f"public:{name}",),
                )
            )
    unresolved = [
        f"허용 설계 {target} 를 다시 만든 뒤에도 공개 테스트 {', '.join(tests)} 와 숨은 케이스 "
        f"{', '.join(_hidden_failures(run))} 를 함께 틀림 — 따로 만든 두 테스트 묶음이 함께 틀렸기보다 구현이 틀렸을 가능성이 커 "
        "테스트를 바꾸지 않음 (사람 확인)"
        for target, (run, tests) in held.items()
    ]
    return out, trusted, unresolved


def _shared_cause(
    runs: Sequence[TargetRun], shared: str = "", terms: RepairTerms | None = None
) -> list[str]:
    """따로 만든 공개 테스트와 숨은 테스트가 모든 구현에서 **전부 같은 오류**로 실패 — 한 생성기를 다시 불러서는 설명되지 않는다.
    공개 테스트(gen_public)·숨은 테스트(gen_hidden)·참조 구현(구현마다)은 서로 따로 만들어지므로, 이들을 모두 같은 방식으로 깨는 것은
    함께 기대는 것(하네스·계약·템플릿)이다 — 다시 만들지 않고 승인자에게 (failed + review_flags). 2026-11-15 프론트 빌드: React 템플릿
    v12 하네스가 `/items/:id` 처리기를 나중에 등록해 `/items/list` 요청을 가로채, 모든 구현의 모든 공개·숨은 테스트가 '요청이 오지 않음'
    으로 같게 실패했는데 '테스트 의심' 으로 공개 테스트만 다시 만들어 예산을 썼다.
    서버 쪽 예외(repair_terms.server_side)는 다시 만들 수 있는 공유 코드(시작 코드)라 여기서 멈추지 않는다 (_public_refs → gen_starter)."""
    if len(runs) < 2:
        return []
    msgs: list[str] = []
    for r in runs:
        tests = [*r.results("public"), *r.results("hidden")]
        if (
            not r.results("public")
            or not r.results("hidden")
            or any(t.result == "PASS" for t in tests)
        ):
            return []
        msgs += [t.message for t in tests]
    if len({_signature(m) for m in msgs}) != 1:
        return []
    if terms is not None and terms.server_side and re.search(terms.server_side, msgs[0]):
        return []
    return [
        f"모든 구현({len(runs)}개)이 공개·숨은 테스트 전부에서 같은 오류로 실패 — 따로 생성한 테스트를 함께 실패시키는 공통 원인"
        f"(하네스·계약·템플릿) 의심, 다시 생성하지 않음: {_short(msgs[0])}{shared}"
    ]


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
        if exhausted(regen_counts, "gen_hidden"):
            return [], [
                f"숨은 테스트를 다시 생성한 뒤에도 기능 없는 시작 코드(공개 테스트 {', '.join(missing)} 실패)가 숨은 케이스 {ids} 를 통과 "
                "— 그 케이스가 기능 단계에 도달하지 않는다 (숨은 테스트·계약 사람 확인)"
            ]
        return [
            RepairAction(
                "gen_hidden",
                "",
                "②'",
                f"기능 없는 시작 코드가 숨은 케이스 {ids} 통과 — 숨은 테스트가 기능 단계에 도달하지 않음",
                f"시작 코드 그대로(새 기능 없음 — 공개 테스트 {', '.join(missing)} 실패)로 숨은 케이스 {ids} 를 통과했다 — 이 케이스는 "
                "아무것도 구현하지 않아도 통과한다. " + (terms or RepairTerms()).feature_first,
            )
        ], []
    if exhausted(regen_counts, "gen_starter"):
        return [], [
            f"시작 코드를 다시 생성한 뒤에도 시작 코드 그대로 숨은 케이스 {ids} 를 통과 — 그 케이스가 지원자의 작업을 측정하지 않을 수 있다 "
            "(시작 코드 범위·숨은 테스트 사람 확인)"
        ]
    return [
        RepairAction(
            "gen_starter",
            "",
            "②'",
            f"시작 코드만으로 숨은 케이스 {ids} 통과 — 새 기능을 이미 포함함",
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
                    pool=COMPILE,
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
                    pool=COMPILE,
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
                    pool=COMPILE,
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
                pool=COMPILE,
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
    깨진 테스트가 모두 시작 코드가 쓴 것이면 표시하기 전에 시작 코드를 한 번 다시 만든다 (_starter_test_first, 2026-11-24).
    """
    if len(runs) < 2:
        return [], set()
    per_run = [{t.name: _signature(t.message) for t in _broke_existing(r, starter)} for r in runs]
    common = set(per_run[0])
    for broke in per_run[1:]:
        common &= {name for name, sig in broke.items() if sig == per_run[0].get(name)}
    flags = [
        f"계약 모호 의심 — 참조 구현 {len(runs)}개가 모두 기존 테스트 {name} 를 같은 오류로 실패시킴: {_short(next(t.message for t in runs[0].tests if t.name == name and t.group == 'existing').rsplit('Exception: ', 1)[-1])} "
        "— 계약에서 그 경우(필드 값·기록 여부)를 정해야 한다 (구현을 다시 생성하지 않음)" + shared
        for name in sorted(common)
    ]
    return flags, common


# 시작 코드의 기존 테스트를 참조 구현이 모두 깰 때 시작 코드에 붙이는 규칙 (_starter_test_first · _starter_test_broken_by_all)
_STARTER_TEST_RULE = (
    "기존 테스트는 계약이 정한 이미 있는 동작만 단언한다. 새 기능 자리의 "
    "미구현 상태(예외·실패 종료·빈 결과)나 계약에 없는 동작을 단언하지 않는다"
)


def _starter_wrote(name: str, starter: Overlay | None) -> bool:
    """기존 테스트 이름이 시작 코드가 쓴 테스트 파일인가 — 이름은 스택마다 파일 경로(Python `tests/test_cli.py`)·
    파일 이름(React `App.test.tsx`)·클래스 이름(Java `OrderControllerTest`). 템플릿에 있던 테스트는 시작 코드로 못 고친다."""
    if starter is None:
        return False
    return any(
        f.path == name or f.path.endswith("/" + name) or PurePosixPath(f.path).stem == name
        for f in starter.files
    )


def _starter_test_first(
    runs: Sequence[TargetRun],
    starter: TargetRun,
    names: Collection[str],
    overlay: Overlay | None,
    counts: Mapping[str, int],
) -> list[RepairAction]:
    """계약 모호 의심(_contract_ambiguity)이어도 깨진 기존 테스트가 **모두 시작 코드가 쓴 것**이면 시작 코드부터 한 번 다시 만든다
    [확정: 2026-11-24 사용자 — 11-07 '계약 모호면 다시 만들지 않음'을 이 경우만 바꿈]. 그 테스트가 새 기능 자리의 미구현 상태를
    단언했으면 기능을 채운 구현은 모두 같은 오류로 깬다 — 스택과 무관한 안전망 (Python 은 정적 검사 _starter_stub_tests 가 먼저 막는다).
    참조 구현은 새 시작 코드 위에 처음부터 다시 쓰인다 (한 회차). 다시 만든 뒤에도 같으면(시작 코드 예산 소진) 예전처럼 계약 모호,
    템플릿 테스트가 섞였으면 시작 코드로 못 고치므로 바로 계약 모호.
    2026-11-24 #256 재료 C 알파 2회: 시작 코드가 '옵션을 주면 종료 코드 1 · NotImplementedError' 를 단언해 참조 구현 셋이 같은 오류로 깼고
    다시 만들지 않고 빌드가 끝났다 (11-15 g187c 는 오류가 서로 달라 _starter_test_broken_by_all 로 갔다)."""
    if not names or exhausted(counts, "gen_starter"):
        return []
    if not all(_starter_wrote(n, overlay) for n in names):
        return []
    broke = {t.name: t.message for t in _broke_existing(runs[0], starter)}
    lines = "\n".join(f"- {n}: {_short(broke.get(n, ''))}" for n in sorted(names))
    return [
        RepairAction(
            "gen_starter",
            "",
            "②",
            f"참조 구현 {len(runs)}개가 모두 시작 코드가 쓴 기존 테스트 {', '.join(sorted(names))} 를 같은 오류로 실패시킴 "
            "— 계약 모호로 보기 전에 시작 코드를 다시 생성",
            f"시작 코드가 쓴 기존 테스트를 따로 만든 참조 구현이 모두 같은 오류로 깬다 — {_STARTER_TEST_RULE}. "
            "그런 테스트만 고치고, 계약이 정한 동작을 확인하는 테스트는 지우거나 약하게 바꾸지 않는다:\n"
            + lines,
        )
    ]


def _starter_test_broken_by_all(
    runs: Sequence[TargetRun], starter: TargetRun, skip: set[str], counts: Mapping[str, int]
) -> list[RepairAction]:
    """참조 구현 둘 이상이 **다시 만든 뒤에도 모두** 시작 코드에서 통과하던 같은 기존 테스트를 깸 — 오류는 서로 달라도 (같은 오류면 _contract_ambiguity).
    따로 만든 구현이 거듭 모두 같은 테스트를 깨면 그 테스트가 계약에 없는 동작을 단언한 것이다 — 구현이 아니라 시작 코드(그 테스트)를 다시 만든다.
    첫 회차는 예전대로 구현을 다시 만든다 (2026-11-07 스모크: 흔한 구현이 필수 필드를 빠뜨린 결함이었다 — 문서 위 3번).
    2026-11-15 #187 데이터 알파(g187c-de-kb002): 시작 코드 기존 테스트가 '진입점이 NotImplementedError 로 실패'를 단언해 구현 넷이 모두 깼는데,
    실패 메시지에 구현마다 다른 실행 출력이 붙어 '같은 오류'로 묶이지 않아 구현 넷을 하나씩 다시 만들다 예산을 썼다."""
    if len(runs) < 2 or any(counts.get(f"{_gen_for(r)}:{r.target}", 0) < 1 for r in runs):
        return []
    per_run = [{t.name: t.message for t in _broke_existing(r, starter)} for r in runs]
    names = sorted(set.intersection(*(set(b) for b in per_run)) - skip)
    if not names:
        return []
    lines = "\n".join(f"- {n}: {_short(per_run[0][n])}" for n in names)
    return [
        RepairAction(
            "gen_starter",
            "",
            "②",
            f"참조 구현 {len(runs)}개가 모두 기존 테스트 {', '.join(names)} 를 실패시킴 — 그 테스트가 계약에 없는 동작을 단언",
            f"시작 코드의 기존 테스트를 따로 만든 참조 구현이 모두 깬다 — {_STARTER_TEST_RULE}:\n"
            + lines,
        )
    ]


def _starter_test_context(
    runs: Sequence[TargetRun], starter: TargetRun, terms: RepairTerms | None
) -> list[RepairAction]:
    """참조 구현 둘 이상이 **모두** 시작 코드에서 통과하던 같은 기존 테스트를 '라우터·Provider 밖' 오류(manifest repair_terms.test_context)로
    깨면 그 테스트가 화면을 맥락 없이 그린 것 — 구현이 useNavigate 등을 쓰는 순간 깨진다. 계약 모호(_contract_ambiguity)보다 먼저 본다."""
    if terms is None or not terms.test_context or len(runs) < 2:
        return []
    rx = re.compile(terms.test_context)
    broke = [{t.name: t.message for t in _broke_existing(r, starter)} for r in runs]
    names = sorted(
        n
        for n in broke[0]
        if all(n in b and rx.search(b[n]) for b in broke) and rx.search(broke[0][n])
    )
    if not names:
        return []
    msg = _short(broke[0][names[0]])
    return [
        RepairAction(
            "gen_starter",
            "",
            "②",
            f"참조 구현 {len(runs)}개가 모두 기존 테스트 {', '.join(names)} 를 같은 맥락 오류로 실패시킴 — 시작 코드 테스트가 라우터·Provider 없이 화면을 렌더링함",
            f"시작 코드의 기존 테스트 {', '.join(names)} 가 화면 컴포넌트를 라우터(·데이터 Provider) 밖에서 그려, 화면이 이동 기능(useNavigate 등)을 쓰면 깨진다: {msg} — "
            "기존 테스트에서 화면을 그릴 때는 MemoryRouter(TanStack Query 를 쓰면 QueryClientProvider 도) 안에서 그린다.",
        )
    ]


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
            f"{run.target} 가 기존 테스트를 실패시킴: " + ", ".join(t.name for t in broke),
            "시작 코드에서 통과하던 기존 기능 테스트가 이 구현에서 실패한다 — 기존 기능·필수 필드를 지킨다:\n"
            + lines,
        )
    ]


def _test_defect(msgs: Mapping[str, str], terms: RepairTerms | None) -> list[str]:
    """하네스가 테스트 결함이라고 짚은 실패 메시지(manifest repair_terms.test_defect)를 낸 대상들."""
    if terms is None or not terms.test_defect:
        return []
    rx = re.compile(terms.test_defect)
    return [t for t, m in msgs.items() if rx.search(m)]


def _hidden_broken(
    runs: Sequence[TargetRun],
    plan: HiddenPlan,
    terms: RepairTerms | None = None,
    regen_counts: Mapping[str, int] | None = None,
) -> list[RepairAction]:
    """숨은 케이스가 판정까지 못 감(테스트 자체 예외·결과 없음) — **구현끼리 비교해** 탓을 정한다.

    - 준비된 구현 **모두**에서 그러면 숨은 테스트 탓 → `gen_hidden`.
    - 일부 구현에서만 그러고 나머지는 판정까지 가면(통과·단언 실패) 그 구현 탓 → `gen_naive`/`gen_accepted`.
      예: 그 구현만 계약과 다른 응답 모양(목록 대신 `{content: …}` 페이지 객체)을 돌려 숨은 테스트가 파싱하다 예외.
    준비된 구현이 하나뿐이면 비교할 수 없어 숨은 테스트 탓 (예전과 같음).
    - 이미 다시 만든 허용 구현이 또 혼자 판정까지 못 가면(다른 구현은 판정까지 감) 숨은 테스트가 한 구현 방식에 기댄 것을 한 번 의심
      (게이트 ④·⑤ 의 '설계 모순 의심'과 같은 예산). 2026-11-11 fe8 오프라인 hc2: 첫 요청을 붙잡은 채 두 번째를 기다려, 하나씩 보내는
      허용 설계만 시간 초과로 떨어질 수 있었다 — 게이트 ④ 는 단언 실패만 보므로 여기서 받는다.
    """
    counts = regen_counts or {}
    out: list[RepairAction] = []
    for c in plan.cases:
        bad = [
            (r, x) for r in runs if (x := r.case(c.case_id)) is None or x.result == "HARNESS_ERR"
        ]
        if not bad:
            continue
        defect = _test_defect({r.target: x.message for r, x in bad if x is not None}, terms)
        if defect:
            msg = next(x.message for r, x in bad if x is not None and r.target == defect[0])
            out.append(
                RepairAction(
                    "gen_hidden",
                    "",
                    "④",
                    f"숨은 케이스 {c.case_id} 가 하네스가 지적한 테스트 결함으로 실패 ({', '.join(defect)})",
                    f"하네스가 숨은 케이스 {c.case_id} 의 결함을 짚었다: {_short(msg)} — 구현 탓이 아니다. 테스트를 고친다.",
                )
            )
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
        again = next(
            (
                (r, x)
                for r, x in bad
                if r.kind == "ref_accepted"
                and exhausted(counts, "gen_accepted", r.target)
                and not exhausted(counts, "gen_hidden", pool=DESIGN)
            ),
            None,
        )
        if again is not None:
            # 테스트를 의심하는 회차에는 이 케이스의 다른 구현(흔한 구현 등)을 다시 만들지 않는다 — 고친 테스트로 다음 회차에 다시 비교한다
            r, x = again
            what = (
                "실행되지 않았다"
                if x is None
                else f"단언이 아닌 예외로 끝났다: {_short(x.message)}"
            )
            out.append(
                RepairAction(
                    "gen_hidden",
                    "",
                    "④",
                    f"다시 생성한 {r.target} 만 {c.case_id} 테스트 예외 — 설계와 모순 의심",
                    f"허용 설계 구현({r.target})을 다시 만들어도 숨은 케이스 {c.case_id} 가 이 구현에서만 {what} — 다른 구현({fine})은 "
                    "판정까지 갔다. 숨은 테스트가 한 구현 방식의 진행(처리 순서·시점·횟수)을 전제로 기다리거나 막혀 있지 않은지 본다: "
                    "구현마다 다를 수 있는 진행을 기다리지 말고, 모든 허용 설계에서 같은 최종 결과(상태·기록·응답)로 판정한다. "
                    "흔한 구현은 계속 의도한 사유로 실패해야 한다.",
                    pool=DESIGN,
                )
            )
            continue
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


def marker_hits(overlay: Overlay | None, markers: Sequence[str]) -> list[str]:
    """흔한 구현 코드(테스트 제외)에 나온 정답 장치 표지 — 재료 fix_markers."""
    if overlay is None or not markers:
        return []
    text = "\n".join(
        f.content for f in overlay.files if "/test/" not in f.path and ".test." not in f.path
    )
    return sorted({m.group(0) for rx in markers for m in re.finditer(rx, text)})[:6]


def _gate3(
    run: TargetRun,
    plan: HiddenPlan,
    missed: set[str],
    regen_counts: Mapping[str, int],
    terms: RepairTerms | None = None,
    hits: Sequence[str] = (),
    family: Collection[str] = (),
    calibrate: Collection[str] = (),
) -> tuple[list[RepairAction], list[str], list[str], list[str]]:
    """흔한 구현의 숨은 케이스 결과로 숨은 테스트·흔한 구현을 고친다. 넷째 값은 조절값부터 맞춰 볼 케이스 (#301 PR 2).
    calibrate: 보정할 수 있는 케이스 (빌드가 아직 보정하지 않았고 그 사유에 숨긴 조절값이 있음 — starter/calibrate.knobs).
    흔한 구현이 허용 설계 장치 없이 그 케이스를 통과하면 다시 만들기 전에 보정을 요청한다 — 흔한 구현은 맞는데 조절값 때문에
    안 떨어질 수 있다 (2026-11-24 세션 유지 알파: 흔한 구현 2회·숨은 테스트 1회 재생성 뒤 실패, 대기 한도·지연만 바꾸자 떨어짐).
    family: 재료가 정한 이 숨은 요구의 사유들(FeatureSeed.reasons) — 흔한 구현이 의도한 사유 대신 같은 묶음의 다른 사유로 떨어져도
    함정을 잡은 것으로 본다. 2026-11-11 재측정 오프라인: 첫 재연결 500 에서 흔한 구현은 메모를 겹치지 않고 잃었다(lost) — hc2(duplicated)를
    세 번 다시 만들게 할 일이 아니다. 대신 의도한 사유가 어느 케이스에서도 안 보이면 승인자 표시로 남긴다.
    숨은 요구마다 따로 판정한다 (#242) — 함정을 보였나·의도한 사유로 잡은 케이스·관찰한 사유는 그 숨은 요구의 케이스에서만 센다.
    한꺼번에 세면 다른 숨은 요구의 케이스가 이 숨은 요구를 잡은 것으로 쳐져, 이 숨은 요구의 케이스가 모두 '변별 못 함'으로 채점에서
    빠지거나 흔한 구현을 먼저 다시 만들 기회를 놓친다. 숨은 요구가 하나면 예전과 같다."""
    fam = set(family)

    def same(intended: str, got: str | None) -> bool:
        return got == intended or (got is not None and got in fam and intended in fam)

    def asserted(trap: str) -> list[tuple[HiddenCase, TestResult]]:
        """그 숨은 요구의 케이스 중 흔한 구현이 단언에서 떨어진 것 (계획 순서)."""
        return [
            (c, r)
            for c in plan.cases
            if c.trap_id == trap and (r := run.case(c.case_id)) is not None and r.result == "ASSERT"
        ]

    passed = [
        c
        for c in plan.cases
        if c.trap_id in missed and (r := run.case(c.case_id)) is not None and r.result == "PASS"
    ]

    # 같은 숨은 요구의 다른 케이스에서 의도한 사유(그 숨은 요구의 어느 케이스든)로 떨어졌으면 흔한 구현은 함정을 갖고 있다 — 통과한
    # 케이스가 그 함정을 못 잰 것이니 흔한 구현을 다시 만들지 않는다. 2026-11-09 v19-loop1: 흔한 구현(따로 둔 AuditService 의
    # REQUIRES_NEW)이 hc2·hc3·hc4 를 rollback_left_success 로 떨어졌는데 hc1 만 통과해 흔한 구현을 다시 만들었고, 다시 만든 판은
    # REQUIRES_NEW 를 같은 클래스 안에서 불러(프록시를 안 거쳐 무효) 함정을 잃고 숨은 케이스를 모두 통과했다.
    def shows_trap(trap: str) -> bool:
        intended = {c.intended_reason for c in plan.cases if c.trap_id == trap} | fam
        return any(r.reason in intended for _, r in asserted(trap))

    # 통과한 케이스 중 그 숨은 요구에 함정을 보인 케이스가 없는 것
    unshown = [c for c in passed if not shows_trap(c.trap_id)]
    if unshown and hits:
        # 흔한 구현이 숨은 케이스를 통과했고 그 코드에 허용 설계의 장치가 있다 — 숨은 테스트가 아니라 흔한 구현(또는 재료) 문제.
        # 2026-11-10 응답 순서 cf: 흔한 구현이 매 회차 React 정리 패턴(let cancelled …)을 썼는데 ③ 이 숨은 테스트를 다시 만들었다.
        ids = ", ".join(c.case_id for c in unshown)
        found = ", ".join(hits)
        if not exhausted(regen_counts, "gen_naive", run.target, NAIVE_FIRST):
            return (
                [
                    RepairAction(
                        "gen_naive",
                        run.target,
                        "③",
                        f"흔한 구현이 {ids} 통과 — 정답 장치({found})를 사용함 ({run.target})",
                        f"이 흔한 구현은 숨은 요구를 어기는 구현이어야 하는데 허용 설계의 장치({found})를 썼다 — 그 장치를 빼고 입력의 흔한 구현 "
                        "설명대로만 쓴다 (설명에 없는 장치를 더하지 않는다).",
                        pool=NAIVE_FIRST,
                    )
                ],
                [],
                [],
                [],
            )
        return (
            [],
            [
                f"{MATERIAL_VERDICT}흔한 구현({run.target})이 다시 생성한 뒤에도 허용 설계의 장치({found})를 사용해 숨은 케이스 {ids} 를 통과 — "
                "AI 흔한 구현이 기본으로 정답을 사용하므로 이 함정은 AI 첫 답을 구별하지 못할 수 있다. 재료의 숨은 요구·흔한 구현 설명 확인 (사람)"
            ],
            [],
            [],
        )
    # 허용 설계 장치 없이 통과 — 다시 만들기 전에 그 사유의 숨긴 조절값부터 실험으로 맞춘다 (#301 PR 2, 빌드가 한 번만)
    tune = [c.case_id for c in unshown if c.case_id in calibrate]
    if tune:
        return [], [], [], tune
    if unshown and not exhausted(regen_counts, "gen_naive", run.target, NAIVE_FIRST):
        # 흔한 구현이 숨은 케이스를 통과 — 테스트가 약한지보다 흔한 구현이 설명을 안 따랐는지를 먼저 본다.
        # 2026-11-07 배송 감사 재실행: 흔한 구현이 REQUIRES_NEW 를 상태 변경 트랜잭션이 끝난 뒤 불러 커넥션을 하나씩만 썼다.
        # 숨은 테스트는 맞았다 (진짜 흔한 구현으로 바꾸자 hc1·hc2 모두 의도한 사유로 실패). 이 회차에는 이 대상의 다른 ③ 배정을 미룬다 —
        # 흔한 구현이 틀렸으면 사유 불일치도 그 탓일 수 있다 (같은 재실행의 hc2 record_missing).
        ids = ", ".join(c.case_id for c in unshown)
        traps = ", ".join(sorted({c.trap_id for c in unshown}))
        return (
            [
                RepairAction(
                    "gen_naive",
                    run.target,
                    "③",
                    f"흔한 구현이 {ids} 통과 — 흔한 구현을 먼저 다시 ({run.target})",
                    f"이 흔한 구현은 숨은 요구 {traps} 를 어기는 구현이어야 하는데 숨은 케이스 {ids} 를 통과했다 — 입력의 흔한 구현 설명을 "
                    "구조 그대로 따르지 않았다. 설명이 말하는 그 구조를 그대로 쓰고, 설명에 없는 장치를 더하거나 "
                    "더 안전한 방식으로 바꾸지 않는다.",
                    pool=NAIVE_FIRST,
                )
            ],
            [],
            [],
            [],
        )
    # 함정을 다른 케이스가 이미 잡는데 숨은 테스트를 다시 만든 뒤에도 흔한 구현이 통과하는 케이스 — 변별하지 못하므로 채점에서 뺀다
    # (RepairPlan.weak → build 가 숨은 계획·숨은 테스트에서 지운다) [확정: 2026-11-11 사용자]. 2026-11-11 풀 교착 hc2: 요청 하나로
    # 잘못된 전이의 실패 기록을 봐 흔한 구현도 통과 — 함정(동시 요청에서 커넥션 고갈)이 터지는 조건을 재현하지 않았다.
    # 2026-11-10 gen-item2: hc1·hc2·hc3 이 의도한 사유로 잡는 t1 을 hc4(record_missing)가 다시 만든 뒤에도 못 잡아 예산을 다 쓰고
    # 루프가 failed — 허용 구현 3개는 숨은 케이스 4개를 모두 통과했다 (흔한 구현의 함정은 기록 누락이 아니라 연결 고갈).
    # 잡은 케이스는 같은 숨은 요구의 것만 (#242) — 다른 숨은 요구의 케이스로 빼면 이 숨은 요구의 케이스가 하나도 안 남을 수 있다.
    caught = {
        t: sorted(c.case_id for c, r in asserted(t) if same(c.intended_reason, r.reason))
        for t in {c.trap_id for c in passed}
    }
    flags: list[str] = []
    weak: list[str] = []  # 채점에서 뺄 케이스 (변별하지 못함)
    # (케이스, 숨은 요구, 의도한 사유, 대신 관찰한 사유)
    substituted: list[tuple[str, str, str, str]] = []
    rewritten = exhausted(regen_counts, "gen_hidden")  # 숨은 테스트를 이미 다시 만들었다
    out: list[RepairAction] = []
    for c in plan.cases:
        r = run.case(c.case_id)
        if r is None or r.result == "HARNESS_ERR":
            continue
        if c.trap_id in missed:
            if r.result == "PASS" and caught[c.trap_id] and rewritten:
                weak.append(c.case_id)
                flags.append(
                    f"숨은 케이스 {c.case_id} 는 숨은 테스트를 다시 생성한 뒤에도 흔한 구현({run.target})이 통과 — 같은 숨은 요구 {c.trap_id} 는 "
                    f"{', '.join(caught[c.trap_id])} 가 의도한 사유로 검출한다. {c.case_id} 는 {WEAK_FLAG} "
                    f"(무엇을 확인했나: {c.expect[:120]})"
                )
            elif r.result == "PASS":
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
                        "단언하지 않는다 — 그러면 올바른 구현이 떨어진다.",  # 2026-11-07 알파 5회차 (#182: 사례 인용은 주석으로만)
                    )
                )
            elif r.reason != c.intended_reason and same(c.intended_reason, r.reason):
                substituted.append((c.case_id, c.trap_id, c.intended_reason, r.reason or ""))
            elif r.reason != c.intended_reason:
                got = r.reason or "사유 표시 없음"
                call = (terms or RepairTerms()).reason_call
                if r.reason is None and r.result == "ASSERT":
                    # 사유 없는 단언에서 먼저 떨어짐 — 그 단언이 함정의 결과인지 준비 확인인지 갈래를 짚는다 (2026-11-11 재측정 오프라인 hc2:
                    # '서버에 3건 이상'(사유 없음)에서 흔한 구현이 메모를 잃어 2건 — 사유가 붙은 중복 단언까지 가지 못했다)
                    where = f"({r.at}) " if r.at else ""
                    own = ", ".join(sorted(fam)) if fam else c.intended_reason
                    note = (
                        f"숨은 케이스 {c.case_id} 가 흔한 구현에서 사유 표시 없는 단언 {where}에서 먼저 실패했다: {_short(r.message)} — "
                        f"이 단언이 숨은 요구 {c.trap_id} 를 어긴 결과(이 요구의 사유: {own})를 보는 것이면 그 단언에 맞는 사유를 {call} 로 붙이고, "
                        "기능 준비 확인이면 흔한 구현도 지나가게 쓴다 (흔한 구현이 잃거나 늘리는 값을 준비 단계에서 단언하지 않는다). "
                        f"의도한 실패({c.intended_reason})를 가장 먼저 확인하는 단언 순서로."
                    )
                else:
                    note = (
                        f"숨은 케이스 {c.case_id} 가 흔한 구현에서 의도한 사유({c.intended_reason}) 대신 {got} 로 실패했다: "
                        f"{_short(r.message)} — 의도한 실패를 가장 먼저 확인하는 단언 순서로, 사유는 {call} 로 붙인다."
                    )
                out.append(
                    RepairAction(
                        "gen_hidden",
                        "",
                        "③",
                        f"{c.case_id} 사유 {got} ≠ 의도 {c.intended_reason} ({run.target})",
                        note,
                    )
                )
    for cid, trap, want, got in substituted:
        # 의도한 사유를 본 케이스는 같은 숨은 요구에서만 센다 (#242)
        seen = {r.reason for _, r in asserted(trap) if r.reason}
        if want not in seen:
            flags.append(
                f"숨은 케이스 {cid} 는 흔한 구현({run.target})을 {want} 대신 {got} 로 검출함 — 같은 숨은 요구의 사유라 변별로 인정함. "
                f"{want} 를 확인하는 케이스는 없다 (사람 확인)"
            )
    return out, flags, weak, []


def _gate4(
    runs: Sequence[TargetRun],
    plan: HiddenPlan,
    regen_counts: Mapping[str, int],
    naive: Sequence[TargetRun] = (),
    regen_log: Mapping[str, RegenNote] | None = None,
    passed_before: Mapping[str, Collection[str]] | None = None,
) -> tuple[list[RepairAction], list[str]]:
    """regen_log: 산출물 키 → 마지막으로 다시 만든 이유. passed_before: 참조 구현 라벨 → 그 설계의 어느 판이든 지금 숨은 테스트로
    통과한 케이스 (숨은 테스트·시작 코드를 다시 만들면 비운다 — build). 이전 판이 통과한 케이스에서 떨어지면 테스트가 아니라
    구현의 퇴행이다 — 테스트를 의심하지 않고 구현을 다시 만든다 (REGRESS 풀, #183)."""
    actions: list[RepairAction] = []
    unresolved: list[str] = []
    log = regen_log or {}
    before = passed_before or {}

    for c in plan.cases:
        failed = [
            (r, res)
            for r in runs
            if (res := r.case(c.case_id)) is not None and res.result == "ASSERT"
        ]
        if not failed:
            continue
        labels = ", ".join(r.target for r, _ in failed)
        # 따로 만든 허용 구현이 모두 떨어지면 테스트를 의심한다 — 단 모두 그 케이스의 의도한 사유(흔한 구현이 빠지는 바로 그 함정)로
        # 떨어지고 흔한 구현도 그 사유로 잡혔으면 "구현들이 설계를 못 따름"과 "테스트가 올바른 설계까지 함정으로 봄"을 가를 수 없다.
        # 싼 쪽부터 — 허용 구현을 한 번 다시 만든다(아래 구현별 흐름). 다시 만든 뒤에도 모두 같으면(예산 소진) 여기서 테스트를 의심한다
        # (사유 표시가 잘못 붙은 테스트일 수 있어 이 출구는 남긴다 — 2026-11-07 루프). 2026-11-15 b2: 허용 구현 둘이 함정에 빠졌는데
        # 맞던 숨은 테스트를 다시 만들어 깨뜨렸다.
        on_trap = (
            c.intended_reason is not None
            and all(res.reason == c.intended_reason for _, res in failed)
            and any(
                (n_res := n.case(c.case_id)) is not None
                and n_res.result == "ASSERT"
                and n_res.reason == c.intended_reason
                for n in naive
            )
            and not any(exhausted(regen_counts, "gen_accepted", r.target) for r, _ in failed)
        )
        if len(runs) >= 2 and len(failed) == len(runs) and not on_trap:
            # 흔한 구현만 통과 (11-18 뒤집힘 — 계약의 출력 위치가 실행 날짜 폴더라 숨은 케이스가 흔한 구현의 모양을 기대했다)
            flip = bool(naive) and all(
                (n_res := n.case(c.case_id)) is not None and n_res.result == "PASS" for n in naive
            )
            actions.append(
                RepairAction(
                    "gen_hidden",
                    "",
                    "④",
                    f"{ALL_ACCEPTED_FAIL}{c.case_id} 실패"
                    + (" · 흔한 구현만 통과" if flip else ""),
                    f"허용 설계 구현이 모두({labels}) 숨은 케이스 {c.case_id} 에서 실패했다: {_short(failed[0][1].message)} "
                    f"— 허용 설계를 떨어뜨리지 않게 통과 조건({c.expect})만 확인한다."
                    + (
                        " 흔한 구현은 이 케이스를 통과한다 — 통과 조건이 흔한 구현의 모양(출력 위치·실행 범위·읽는 범위)을 "
                        "기대하지 않는지 본다."
                        if flip
                        else ""
                    ),
                    cases=(c.case_id,),
                    blame=(f"hidden:{c.case_id}",),
                )
            )
            continue
        passing = [
            r.target
            for r in runs
            if (got := r.case(c.case_id)) is not None and got.result == "PASS"
        ]
        for r, res in failed:
            if not exhausted(regen_counts, "gen_accepted", r.target):
                actions.append(
                    RepairAction(
                        "gen_accepted",
                        r.target,
                        "④",
                        f"{r.target} 가 {c.case_id} 실패 [{res.reason or '-'}]",
                        f"이 허용 설계 구현이 숨은 요구 {c.trap_id} 의 확인({c.case_id}, 통과 조건: {c.expect})에서 실패했다: "
                        f"{_short(res.message)} — 설계 조건과 스택 조건을 지켜 다시 쓴다.",
                        cases=(c.case_id,),
                    )
                )
            elif c.case_id in before.get(r.target, ()):
                # 이 설계의 이전 판이 같은 숨은 테스트로 통과했다 — 테스트는 이 설계로 통과할 수 있다. 다른 이유로 다시 만든 구현이
                # 퇴행한 것이므로 테스트를 의심하지 않는다 (2026-11-14 naver_2026#002: ⑤ 로 다시 만든 d2 가 같은 번호 동시 요청에 500)
                prev = log.get(f"gen_accepted:{r.target}")
                why = f" (게이트 {prev.gate}: {prev.reason})" if prev is not None else ""
                if not exhausted(regen_counts, "gen_accepted", r.target, REGRESS):
                    actions.append(
                        RepairAction(
                            "gen_accepted",
                            r.target,
                            "④",
                            f"다시 생성한 {r.target} 가 이전 판이 통과하던 {c.case_id} 에서 실패 [{res.reason or '-'}]",
                            f"이 허용 설계 구현은 다른 이유{why}로 다시 만든 뒤, 이전 판이 통과하던 숨은 요구 {c.trap_id} 의 "
                            f"확인({c.case_id}, 통과 조건: {c.expect})에서 실패했다: {_short(res.message)} — 원래 고치려던 것과 "
                            "이 확인을 함께 지켜 다시 쓴다.",
                            pool=REGRESS,
                            cases=(c.case_id,),
                        )
                    )
                else:
                    unresolved.append(
                        f"허용 설계 {r.target} 를 다시 생성한 뒤 이전 판이 통과하던 {c.case_id} 에서 계속 실패: {_short(res.message)} "
                        "— 숨은 테스트는 이전 판이 통과했으므로 구현 생성의 한계 (사람 확인)"
                    )
            elif passing and not exhausted(regen_counts, "gen_hidden", pool=DESIGN):
                # 다시 만든 허용 구현이 또 떨어지는데 다른 허용 구현은 통과 — 케이스가 한 설계의 진행만 단언했을 수 있다.
                # 흔한 구현을 의도한 사유로 잡는지(아래 갈래)는 이 케이스가 공정한지를 말해 주지 않는다. 한 번만 테스트를 의심한다.
                actions.append(
                    RepairAction(
                        "gen_hidden",
                        "",
                        "④",
                        f"다시 생성한 {r.target} 만 {c.case_id} 실패 — 설계와 모순 의심",
                        f"허용 설계 구현({r.target})을 다시 만들어도 숨은 케이스 {c.case_id} 에서 실패하고, 다른 허용 설계 "
                        f"구현({', '.join(passing)})은 통과한다: {_short(res.message)} — 허용 설계 {r.target} 의 정당한 동작"
                        "(입력의 허용 설계들이 서로 다르게 하는 것 — 진행·횟수·중간 값)을 단언하지 않았는지 본다. "
                        f"모든 허용 설계에서 같은 결과(통과 조건: {c.expect})만 확인하고, 흔한 구현은 계속 의도한 사유로 실패해야 한다.",
                        pool=DESIGN,
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
                    f"허용 설계 {r.target} 가 다시 생성한 뒤에도 {c.case_id} 에서 흔한 구현과 같은 단언·같은 메시지로 실패: {_short(res.message)} "
                    "— 숨은 테스트·계약 의심 (사유 표시가 단언과 일치하는지, 계약에 결과를 정하는 규칙이 있는지 사람 확인)"
                )
            elif res.reason is not None and any(
                (n_res := n.case(c.case_id)) is not None
                and n_res.result == "ASSERT"
                and n_res.reason == c.intended_reason
                for n in naive
            ):
                # 숨은 테스트는 흔한 구현을 의도한 사유로 잡고, 허용 구현은 사유가 붙은 단언(숨은 요구를 재는 단언)에서 떨어진다
                # → 허용 구현이 설계를 못 따른 것. 2026-11-07 수리 루프: 여기서 숨은 테스트를 의심해 다시 만들었더니 맞던 테스트가 컴파일조차 안 됐다.
                # 2026-11-13: 허용 구현이 사유 없는 단언에서 떨어지면 숨은 요구가 아니라 테스트의 다른 조건 탓일 수 있다 — 아래 갈래로 넘겨
                # 테스트를 한 번 의심한다 (2026-11-12 행마다 조회 알파: 흔한 구현은 request_timeout, 허용 구현은 기본 크기 목록에 사유 없는
                # hasSize(100) 으로 떨어졌는데 허용 구현 탓으로 돌아가 '사람 확인'). 다른 사유 코드(예: record_missing)는 계속 허용 구현 탓.
                unresolved.append(
                    f"허용 설계 {r.target} 가 다시 생성한 뒤에도 {c.case_id} 실패 [{res.reason or '-'}] — 숨은 테스트는 흔한 구현을 "
                    f"의도한 사유({c.intended_reason})로 검출하므로 허용 구현이 설계를 준수하지 못함: 설계·스택 조건 보강 (사람 확인)"
                )
            elif len(runs) == 1 and not exhausted(regen_counts, "gen_hidden"):
                # 허용 구현이 하나뿐이라 "모두 실패"로 가를 수 없다 — 구현을 한 번 고쳐도 실패하면 테스트를 의심
                actions.append(
                    RepairAction(
                        "gen_hidden",
                        "",
                        "④",
                        f"다시 생성한 {r.target} 도 {c.case_id} 실패",
                        f"허용 설계 구현({r.target})을 다시 만들어도 숨은 케이스 {c.case_id} 에서 실패한다: {_short(res.message)} "
                        f"— 허용 설계가 통과할 수 있게 통과 조건({c.expect})만 확인한다.",
                    )
                )
            else:
                unresolved.append(
                    f"허용 설계 {r.target} 가 다시 생성한 뒤에도 {c.case_id} 실패 — 설계 조건 부족 또는 숨은 테스트 과함 (사람 확인)"
                )
    return actions, unresolved


def _grading_env(runs: Sequence[TargetRun], terms: RepairTerms | None) -> list[str]:
    """모든 구현이 같은 테스트에서 '채점 환경에 없는 기능' 오류(manifest repair_terms.grading_env)로 떨어지면 — 생성물이 아니라 템플릿
    채점 환경 문제라 무엇을 다시 만들어도 같다. 2026-11-10 프론트 알파 0108be12: jsdom 에 IndexedDB 가 없어 허용 구현 3개·흔한 구현이
    모두 `ReferenceError: indexedDB is not defined` 였는데 '테스트 의심'으로 공개 테스트를 두 번 다시 만들고 예산을 다 썼다."""
    if terms is None or not terms.grading_env or len(runs) < 2:
        return []
    rx = re.compile(terms.grading_env)
    names = sorted({(t.group, t.name) for r in runs for t in r.tests if t.result != "PASS"})
    out: list[str] = []
    for group, name in names:
        msgs = [
            next(
                (
                    t.message
                    for t in r.tests
                    if t.group == group and t.name == name and t.result != "PASS"
                ),
                None,
            )
            for r in runs
        ]
        if all(m is not None and rx.search(m) for m in msgs):
            out.append(
                f"채점 환경 확인 필요 — 구현 {len(runs)}개가 모두 {group} {name} 에서 채점 환경 오류: {_short(msgs[0] or '')} "
                "(테스트·구현을 다시 생성하지 않았다 — 템플릿 채점 환경에 그 기능이 있는지 확인)"
            )
    return out


def assign(
    report: VerifyReport,
    plan: HiddenPlan,
    overlays: Mapping[str, Overlay],
    missed_traps: Mapping[str, set[str]],
    regen_counts: Mapping[str, int] | None = None,
    shared_paths: Collection[str] = (),
    terms: RepairTerms | None = None,
    fix_markers: Sequence[str] = (),
    feature_reasons: Collection[str] = (),
    regen_log: Mapping[str, RegenNote] | None = None,
    passed_before: Mapping[str, Collection[str]] | None = None,
    new_tests: Collection[str] = (),
    calibrate: Collection[str] = (),
    contract: TaskContract | None = None,
) -> RepairPlan:
    """검증 결과 → 다시 만들 생성기 목록. overlays 키는 "starter"·"hidden"·참조 구현 라벨.
    new_tests: 새 API(계약 existing 아님)에만 걸린 공개 테스트 id (pt#) — 시작 코드로 통과하면 새 기능이 들어 있다 (②').
    contract: 시작 코드 기존 테스트가 떨어지면 시드에 없는 조회 값에 실제 값 예를 붙인다 (_seed_hint).
    calibrate: 보정할 수 있는 숨은 케이스 (#301 PR 2) — 흔한 구현이 허용 설계 장치 없이 통과하면 다시 만들기 대신 RepairPlan.calibrate.
    missed_traps: 흔한 구현 라벨 → 그 구현이 놓친 숨은 요구 id (naive-all 은 전부).
    shared_paths: 템플릿·시작 코드에 이미 있는 파일 — 모든 구현이 같은 오류를 낼 때, 모두 다시 쓴 파일을 원인 후보로 싣는다."""
    counts = regen_counts or {}
    res = RepairPlan()
    starter = report.run(STARTER)
    if starter is None:
        res.unresolved.append("시작 코드 검증 결과가 없음")
        return res
    if env := _grading_env(
        [r for r in report.runs if r.kind not in ("starter", "starter_hidden") and r.setup == "ok"],
        terms,
    ):
        res.unresolved += env  # 다시 만들 것이 없다 — 승인자에게 (review_flags)
        return res
    if acts := _starter(starter, overlays, terms, contract):
        res.actions = acts
        return res
    if acts := _conform(report.runs):
        # 계약 모양 응답에 깨진 코드부터 고친다. 깨진 구현을 빼고 남은 구현끼리 비교하면 '모든 구현이 실패'가 구현 하나로도 성립해
        # 테스트를 잘못 의심한다 (2026-11-10 e99cbba0 재검증: 깨진 셋을 빼자 남은 d3 하나의 공개 실패로 공개 테스트를 다시 만들려 했다).
        # 다음 회차에 고친 구현까지 모두 다시 비교한다.
        res.actions = acts
        return res
    acts, flags = _starter_public(starter, counts, new_tests)
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
    if acts := _starter_test_context(ready, starter, terms):
        res.actions = [
            *actions,
            *acts,
        ]  # 시작 코드가 바뀌면 참조 구현은 새 시작 코드 위에 다시 쓰인다
        return res
    ambiguous, skip = _contract_ambiguity(ready, starter, shared)
    if acts := _starter_test_first(ready, starter, skip, overlays.get(STARTER), counts):
        res.actions = [
            *actions,
            *acts,
        ]  # 참조 구현은 새 시작 코드 위에 처음부터 — 다시 만든 뒤에도 같으면 다음 회차에 계약 모호
        return res
    res.unresolved += ambiguous
    if acts := _starter_test_broken_by_all(ready, starter, skip, counts):
        res.actions = [
            *actions,
            *acts,
        ]  # 시작 코드가 바뀌면 참조 구현은 새 시작 코드 위에 다시 검증된다
        return res
    if not actions and (cause := _shared_cause(ready, shared, terms)):
        res.unresolved += cause  # 다시 만들 생성기가 없다 — 예산을 쓰지 않고 승인자에게
        return res
    for run in ready:
        actions += _ref_existing(run, starter, skip)
    public_acts, trusted, held = _public_refs(ready, counts, shared, terms)
    actions += public_acts
    res.unresolved += held
    # 숨은 게이트는 공개 테스트를 통과한 구현만 믿는다 — 공개 요구가 깨진 구현의 숨은 결과는 그 결함 탓일 수 있다
    # (2026-11-08: 시작 코드 결함으로 두 구현이 같은 500 을 내자 흔한 구현이 '의도한 사유'로 실패한 것처럼 보였다)
    judged = [r for r in ready if r.target in trusted]
    actions += _hidden_broken(judged, plan, terms, counts)
    for run in judged:
        if run.kind == "ref_naive":
            hits = marker_hits(overlays.get(run.target), fix_markers)
            acts, flags, weak, tune = _gate3(
                run,
                plan,
                missed_traps.get(run.target, set()),
                counts,
                terms,
                hits,
                feature_reasons,
                calibrate,
            )
            actions += acts
            res.calibrate += [(run.target, c) for c in tune]
            res.unresolved += [f for f in flags if f.startswith(MATERIAL_VERDICT)]
            res.flags += [f for f in flags if not f.startswith(MATERIAL_VERDICT)]
            res.weak += [w for w in weak if w not in res.weak]
    accepted = [r for r in judged if r.kind == "ref_accepted"]
    if accepted:
        acts, unresolved = _gate4(
            accepted,
            plan,
            counts,
            [r for r in judged if r.kind == "ref_naive"],
            regen_log,
            passed_before,
        )
        actions += acts
        res.unresolved += unresolved
    res.actions = [
        replace(a, note=a.note + KEEP_NAIVE)
        if a.generator == "gen_naive" and a.pool in (GENERAL, COMPILE)
        else a
        for a in _merge(actions)
    ]
    res.actions, suspects = _contract_suspects(res.actions, counts, regen_log or {})
    res.unresolved += suspects
    return res


def _contract_suspects(
    actions: Sequence[RepairAction],
    counts: Mapping[str, int],
    log: Mapping[str, RegenNote],
) -> tuple[list[RepairAction], list[str]]:
    """모든 구현이 같은 테스트에서 떨어져 그 테스트를 이미 다시 만들었는데 같은 테스트가 또 — 테스트를 또 만들지 않고 계약 결함
    의심으로 멈춘다 (#256 4단계). 예산이 남아 있지 않아 어차피 멈출 자리지만, '재생성 예산 소진'이 아니라 근거를 남긴다.
    되풀이는 사유 글이 아니라 `blame` 칸(테스트 id)으로 본다 — 사유는 `_merge` 가 합치고 자르며, 뒤집힘 꼬리말이 회차마다 바뀐다."""
    kept: list[RepairAction] = []
    out: list[str] = []
    for a in actions:
        prev = log.get(a.key)
        again = sorted(set(a.blame) & set(prev.blame)) if prev is not None else []
        if (
            a.generator in ("gen_public", "gen_hidden")
            and again
            and exhausted(counts, a.generator, a.label, a.pool)
        ):
            out.append(
                f"{CONTRACT_SUSPECT} — {', '.join(again)} 에서 모든 구현이 그 테스트를 다시 만든 뒤에도 실패: {_short(a.note)} — "
                "계약의 명령·입출력·규칙이 이 테스트를 성립시킬 수 있는지 사람 확인"
            )
            continue
        kept.append(a)
    return kept, out


@dataclass(frozen=True)
class ContractSignal:
    """계약 탓을 가리키는 꼴 — 빌드가 어느 관문에서 멈췄든 마지막 검증에서 (#256 4단계)."""

    kind: Literal["flip", "all_same"]
    group: str  # public · hidden
    name: str  # 공개 테스트 이름 · 숨은 케이스 id
    message: str

    def line(self) -> str:
        what = "공개 테스트" if self.group == "public" else "숨은 케이스"
        msg = f" ({self.message})" if self.message else ""
        if self.kind == "flip":
            return (
                f"{what} {self.name}: 흔한 구현만 통과, 허용 설계 모두 실패{msg} — 계약이 흔한 구현의 모양"
                "(입출력 위치·실행 범위)을 정했는지 확인"
            )
        return f"{what} {self.name}: 모든 구현이 같은 이유로 실패{msg} — 테스트·계약·공유 코드(시작 코드) 결함 의심"


def contract_signals(report: VerifyReport, plan: HiddenPlan) -> list[ContractSignal]:
    """마지막 검증의 계약 탓 꼴 — 흔한 구현만 통과하는 숨은 케이스, 모든 구현이 같은 서명으로 떨어지는 테스트.
    2026-11-18 woori001: 수리가 공개 pt3(crontab)에서 멈춰 hc1 의 뒤집힘(흔한 구현만 통과)이 실패 사유에 나오지 않았다."""
    refs = [r for r in report.runs if r.kind in ("ref_naive", "ref_accepted") and r.setup == "ok"]
    naive = [r for r in refs if r.kind == "ref_naive"]
    accepted = [r for r in refs if r.kind == "ref_accepted"]
    out: list[ContractSignal] = []
    flipped: set[tuple[str, str]] = set()
    if naive and accepted:
        for c in plan.cases:
            acc = [r.case(c.case_id) for r in accepted]
            if all(t is not None and t.result == "ASSERT" for t in acc) and all(
                (t := n.case(c.case_id)) is not None and t.result == "PASS" for n in naive
            ):
                first = acc[0]
                assert first is not None
                flipped.add(("hidden", c.case_id))
                out.append(ContractSignal("flip", "hidden", c.case_id, _short(first.message)))
    if len(refs) >= 2:
        names = sorted(
            {
                (t.group, t.name)
                for r in refs
                for t in r.tests
                if t.group in ("public", "hidden") and t.result != "PASS"
            }
        )
        for group, name in names:
            got = [
                next((t for t in r.tests if t.group == group and t.name == name), None)
                for r in refs
            ]
            bad = [t for t in got if t is not None and t.result != "PASS"]
            if (
                len(bad) == len(refs)
                and len({_signature(t.message) for t in bad}) == 1
                and (group, name) not in flipped
            ):
                out.append(ContractSignal("all_same", group, name, _short(bad[0].message)))
    return out
