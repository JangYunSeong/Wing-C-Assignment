"""빌드 조절값 보정 (#301 PR 2) — 흔한 구현이 숨은 케이스를 통과했을 때 다시 만들기 전에, 그 사유의 tunes(숨긴 조절값)만 바꿔
흔한 구현·허용 구현을 다시 돌려 본다. '허용 구현은 모두 통과하고 흔한 구현은 의도한 사유로 떨어지는' 값의 창을 실험으로 찾는다 — LLM 없음.

[확정: 2026-11-25 사용자 (#301 방향)] 무엇을 조절할지는 템플릿(tunes), 얼마로 할지는 실험이 정한다. 최종 판정은 늘 ③.
2026-11-24 세션 유지 알파: 흔한 구현(요청마다 DB 문장 약 4개)이 Hikari 기본 대기 30초에서 숨은 케이스를 통과했고, 같은 코드가 대기 한도
250~400ms·지연 30~50ms 에서 pool_acquire_timeout 으로 떨어졌다 — 흔한 구현 2회·숨은 테스트 1회 재생성은 헛수고였다.

찾는 법: 한도 값(대기 한도)은 넉넉한 쪽부터 사다리를 내려가고, 부하 값(지연)은 계획 값 먼저 — 창이 없거나 좁으면 한 단계 올린다.
한 부하 단계에서 허용 구현이 떨어지기 시작하면 그 아래 한도는 보지 않는다 (한도가 짧을수록 허용 구현에 불리).
창 = [허용 구현이 모두 통과한 가장 작은 한도, 흔한 구현이 의도한 사유로 떨어진 가장 큰 한도]. 비율이 MIN_RATIO 이상이면 기하 중간값 —
채점은 받는 쪽 기계에서 돌아 시간 기준이 달라질 수 있어 양쪽에 여유를 둔다 (지연은 인위적 sleep 이라 대체로 옮겨진다).
대기열 모형 추정(#301): 지연 20ms 는 창이 Hikari 하한(250)에 눌려 [250, 250] 처럼 좁고, 50ms 에서 [250, 500] 이 된다.

시험 한 번은 흔들린다 (#308 운영 b7, 2026-11-25): 보정 시험 cal7(500ms·50ms)에서 흔한 구현이 한 번 통과해 창이 [250, 250] 으로 줄어
빌드가 실패했는데, 같은 코드·조건 재생에서는 500ms 에서 5번 중 3번 떨어지고 350ms 에서는 10번 모두 떨어졌다. 그래서 두 곳만 반복한다 —
창을 MIN_RATIO 까지 넓힐 바로 위 한도에서 흔한 구현이 통과했으면 그 한도를 EDGE_RETRIES 번 더 돌리고 (한 번이라도 떨어지면 창 끝),
고른 값은 CONFIRM 번 돌려 흔한 구현이 모두 떨어지고 허용 구현이 모두 통과할 때만 고른다. 허용 구현 쪽 실패는 다시 돌리지 않는다 —
허용 구현이 흔들리는 한도는 지원자에게도 흔들리므로 창에 넣지 않는 쪽이 안전하다.
"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from task_spec.schemas.build import HiddenPlan
from task_spec.schemas.domain import TaskItem
from task_spec.schemas.template import HarnessParam, TemplateManifest
from task_spec.schemas.verify_report import VerifyReport
from task_spec.starter.render import render_params
from task_spec.starter.static_check import Overlay

# 한도 조절값 → 시도할 값 (넉넉한 쪽부터). 대기 한도는 Hikari 하한 250 아래로 가지 않는다 — 1~249 는 기동 실패 (#301 하네스 실험).
# 사다리가 없는 tunes 값(예: request_timeout_s)은 아직 후보를 정하지 않아 보정하지 않는다 — 그 사유는 기존 재생성 경로로.
THRESHOLDS: dict[str, tuple[int, ...]] = {"connection_timeout_ms": (2000, 1000, 500, 250)}
# 사다리 맨 위에서 허용 구현이 떨어질 때 올려 볼 더 느슨한 한도 (낮은 쪽부터) — 버스트 부하가 무거우면 2000 에서도 허용 구현이 떨어졌다
# (#337 7절, #334 알파 2·3회차 동시/풀 82/20·124/20). 8000 이면 요청 한도는 max(30, 8000×2/1000+5) = 30초 그대로
LOOSER: dict[str, tuple[int, ...]] = {"connection_timeout_ms": (4000, 8000)}
# 부하 조절값 → 계획 값으로 창이 안 나올 때 차례로 올려 볼 값
LOADS: dict[str, tuple[int, ...]] = {"query_delay_ms": (50,)}
MIN_RATIO = 2.0
EDGE_RETRIES = 2  # 창 끝 한도를 더 돌려 볼 횟수 (첫 시험 포함 최대 3번)
CONFIRM = 2  # 고른 값을 확인할 횟수 — 다음 회차 전체 검증(③)이 한 번 더 본다
MAX_TRIALS = 16  # 부하 단계 2 × (사다리 4 + 창 끝 2 + 확인 2)
STEP = 10  # 고른 한도의 단위
LABEL = "cal"  # 보정 실행 대상 라벨 cal<n>-<라벨> — 검증기 작업 폴더(r<회차>-<라벨>)가 실제 검증·다른 조합과 겹치지 않게
FLAG = "조절값 보정: "  # 승인 화면 표시 머리 (막지 않음 — 승인자가 확인)
OVERLOADED = "허용 구현도 버티지 못하는 부하"  # overloaded 재료 판정의 머리 (#337 7절)
# 명세 글이 조절값을 적는 로그 문구 — 표기 패턴(spec_patterns)은 설정 꼴만 읽어, 2단계 로그의 Hikari 문구
# 'Connection is not available, request timed out after 3000ms' 를 못 읽었다 (2026-11-25 #308: 보정 350ms ↔ 공개 로그 3000ms)
SPEC_LOGS: dict[str, tuple[str, ...]] = {
    "connection_timeout_ms": (r"timed\s+out\s+after\s+(\d+)\s*ms",)
}

Status = Literal["calibrated", "naive_untouched", "no_window", "overloaded", "skipped"]


@dataclass(frozen=True)
class Knobs:
    """한 사유를 보정할 조절값 — 한도(사다리를 내려가며 잰다)와 부하(창이 없으면 올린다)."""

    reason: str
    threshold: str
    ladder: tuple[int, ...]
    load: str | None = None  # 재료가 나열했거나 지금 값을 모르면 None — 계획 값 그대로
    loads: tuple[int, ...] = ()  # 시도할 부하 값 (계획 값 먼저)
    looser: tuple[
        int, ...
    ] = ()  # 사다리 맨 위에서 허용 구현이 떨어질 때 올려 볼 한도 (LOOSER, #337 7절)


@dataclass(frozen=True)
class Trial:
    """조합 하나를 돌린 결과."""

    values: Mapping[str, int]
    # 흔한 구현이 요청한 케이스 중 하나라도 의도한 사유(같은 묶음 사유 포함)로 떨어뜨림 — 케이스마다 따로 (#337 7절: '모두'로 보면
    # 누구에게나 가벼운 케이스 하나 때문에 떨어진 케이스까지 '흔한 구현이 안 떨어짐'이 됐다)
    caught: bool
    clean: bool  # 허용 구현이 모두 모든 테스트 통과
    caught_cases: tuple[str, ...] = ()  # 흔한 구현이 떨어진 케이스 (judge — 비면 알 수 없음)


@dataclass(frozen=True)
class Calibration:
    """보정 한 번의 결과 — calibrated 면 values 를 숨은 계획에 넣는다.
    naive_untouched: 어느 조합·케이스에서도 흔한 구현이 떨어지지 않음 (흔한 구현이 함정을 안 가졌을 수 있다 → 기존 재생성 경로).
    no_window: 흔한 구현은 떨어지는데 허용 구현이 모두 통과하는 넉넉한 창이 없음 (재료가 조절값으로 재현되지 않음 → 사람).
    overloaded: 더 느슨한 한도까지 올려도 허용 구현이 떨어짐 — 버스트가 읽는 행 수·동시 요청이 허용 설계도 버티지 못함
    (#337 7절 → 사람, 숨은 케이스 재시도 없이 바로 재료 판정).
    skipped: 돌릴 수 없음 (검증기·공개 테스트·보정할 값 없음) → 기존 경로."""

    status: Status
    knobs: Knobs | None = None
    values: Mapping[str, int] = field(default_factory=dict)
    window: tuple[int, int] | None = None
    trials: tuple[Trial, ...] = ()
    cases: tuple[str, ...] = ()
    why: str = ""  # skipped 사유
    # 창에서 골랐지만 확인 시험에서 흔한 구현이 통과했거나 허용 구현이 떨어진 한도
    unconfirmed: tuple[int, ...] = ()
    # 요청한 케이스 중 고른 값에서 흔한 구현이 떨어지지 않은 것 (calibrated) — 보정 밖, 다음 회차 기존 경로 (#337 7절)
    missed: tuple[str, ...] = ()

    def flag(self) -> str:
        """승인 화면 표시 (calibrated)."""
        assert self.knobs is not None and self.window is not None
        values = "·".join(f"{name} {value}" for name, value in self.values.items())
        lo, hi = self.window
        hit = [c for c in self.cases if c not in self.missed]
        missed = (
            f" · {'·'.join(self.missed)} 는 흔한 구현이 떨어지지 않아 보정 밖"
            if self.missed
            else ""
        )
        return (
            f"{FLAG}{values} — 흔한 구현이 {'·'.join(hit)} 에서 {self.knobs.reason} 로 실패, 허용 구현 모두 통과 "
            f"({self.knobs.threshold} 창 {lo}~{hi}, 시험 {len(self.trials)}회){missed}"
        )

    def row(self) -> dict[str, Any]:
        """보고서 한 줄 (알파 report.json·재생 결과) — 상태·고른 값·창·조합마다 결과."""
        return {
            "status": self.status,
            "reason": self.knobs.reason if self.knobs is not None else None,
            "cases": list(self.cases),
            "values": dict(self.values),
            "window": list(self.window) if self.window is not None else None,
            "trials": [
                {
                    "values": dict(t.values),
                    "caught": t.caught,
                    "clean": t.clean,
                    "caught_cases": list(t.caught_cases),
                }
                for t in self.trials
            ],
            "why": self.why,
            "unconfirmed": list(self.unconfirmed),
            "missed": list(self.missed),
        }

    def verdict(self) -> str:
        """재료 판정 문구 (no_window·overloaded) — 머리(repair.MATERIAL_VERDICT)는 부르는 쪽이 붙인다."""
        assert self.knobs is not None
        names = "·".join(n for n in (self.knobs.threshold, self.knobs.load) if n)
        if self.status == "overloaded":
            top = max(t.values[self.knobs.threshold] for t in self.trials)
            # 알파 사실(2026-11-26): 보정이 성공한 회차는 버스트가 22행 표를 읽었고, 실패한 회차들은 동시 요청만큼 더한 행이 버스트가
            # 읽는 목록 표에 들어가 400~550행을 매 요청 읽었다 — 명세 수치만의 문제가 아니라 숨은 케이스 버스트 설계를 먼저 본다
            return (
                f"{OVERLOADED} — {self.knobs.threshold} 를 {top} 까지 늘려도 허용 구현이 실패 "
                f"({self.knobs.reason}, {names} 변경, 시험 {len(self.trials)}회) · 숨은 케이스 버스트가 부르는 API 가 읽는 행 수"
                "(동시 요청만큼 더한 행이 그 목록에 들어가는지)와 동시 요청·풀 크기 확인 (사람)"
            )
        hit = sorted(
            {t.values[self.knobs.threshold] for t in self.trials if t.caught}, reverse=True
        )
        shaky = (
            f", 창에서 고른 {', '.join(map(str, self.unconfirmed))} 는 확인 시험에서 흔한 구현 통과 또는 허용 구현 실패"
            if self.unconfirmed
            else ""
        )
        return (
            f"{self.knobs.reason} 를 조절값으로 재현 불가 — {names} 를 변경해도 허용 구현 모두 통과·흔한 구현 실패인 "
            f"{MIN_RATIO:g}배 창 없음 (시험 {len(self.trials)}회, 흔한 구현이 실패한 {self.knobs.threshold}: "
            f"{', '.join(map(str, hit))}{shaky}). 재료의 숨은 요구·흔한 구현 설명 확인 (사람)"
        )


def spec_texts(task: TaskItem) -> list[str]:
    """명세에서 지원자가 보는 글 — 상황, 1단계 자료, 2단계(배포 뒤 증상) 상황·자료. 보정 값과 대조할 수치를 찾는 곳."""
    out = [task.scenario or "", *(a.content for a in task.artifacts)]
    if task.debug_stage is not None:
        out += [task.debug_stage.scenario, *(a.content for a in task.debug_stage.artifacts)]
    return [t for t in out if t]


def spec_values(texts: Sequence[str], name: str, m: TemplateManifest) -> list[int]:
    """명세 글이 그 조절값으로 적은 수치 — 조절값 표기 패턴(그룹 1 = 값) + 로그 문구(SPEC_LOGS)."""
    p = m.param(name)
    patterns = [*(p.spec_patterns if p is not None else []), *SPEC_LOGS.get(name, ())]
    return sorted(
        {
            int(found.group(1))
            for text in texts
            for pattern in patterns
            for found in re.finditer(pattern, text, re.IGNORECASE)
            if found.group(1).isdigit()
        }
    )


def spec_note(name: str, chosen: int, said: Sequence[int]) -> str:
    """보정 값이 명세 글의 수치와 다를 때 승인 화면 표시에 덧붙일 말 — 같거나 명세에 수치가 없으면 빈 글.
    보정 창 밖의 공개 수치에는 값을 맞출 수 없어(예: 창 [250, 500] ↔ 로그 3000ms) 승인자에게 알린다."""
    if not said or chosen in said:
        return ""
    return f" · 명세 자료의 {name} 값({', '.join(map(str, said))})과 다름 — 채점은 보정 값 {chosen} 사용"


def _in_range(value: int, p: HarnessParam | None) -> bool:
    return (
        p is not None
        and (p.minimum is None or value >= p.minimum)
        and (p.maximum is None or value <= p.maximum)
    )


def _current(plan: HiddenPlan, m: TemplateManifest, name: str) -> int | None:
    """계획의 값, 없으면 템플릿 기본값 — 하네스가 실제로 쓰는 값 (모르면 None)."""
    value = plan.harness.get(name)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    p = m.param(name)
    return int(p.default) if p is not None and p.default is not None else None


def knobs(
    reason: str, m: TemplateManifest, plan: HiddenPlan, listed: Collection[str] = ()
) -> Knobs | None:
    """사유의 tunes 중 이 빌드에서 바꿔도 되는 값 — 템플릿에서 숨겼고 재료가 나열하지 않은 값(listed = FeatureSeed.harness).
    빌드의 템플릿은 재료로 좁히지 않아 visible 이 재료 나열을 반영하지 않는다 (#301 팩트체크) — 재료가 나열한 값은 명세에 보이는
    운영 조건이라 바꾸면 명세와 채점이 어긋난다. 한도 사다리가 정해진 값이 없으면 None (보정하지 않음)."""
    r = next((x for x in m.failure_reasons if x.code == reason), None)
    if r is None:
        return None
    free = [
        n for n in r.tunes if n not in listed and (p := m.param(n)) is not None and not p.visible
    ]
    threshold = next((n for n in free if n in THRESHOLDS), None)
    if threshold is None:
        return None
    ladder = tuple(v for v in THRESHOLDS[threshold] if _in_range(v, m.param(threshold)))
    if not ladder:
        return None
    looser = tuple(
        v for v in LOOSER.get(threshold, ()) if v > ladder[0] and _in_range(v, m.param(threshold))
    )
    load = next((n for n in free if n in LOADS), None)
    base = _current(plan, m, load) if load is not None else None
    if load is None or base is None:
        return Knobs(reason, threshold, ladder, looser=looser)
    ups = tuple(v for v in LOADS[load] if v > base and _in_range(v, m.param(load)))
    return Knobs(reason, threshold, ladder, load, (base, *ups), looser)


def window(level: Sequence[Trial], threshold: str) -> tuple[int, int] | None:
    """한 부하 단계의 창 [허용 구현이 모두 통과한 가장 작은 한도, 흔한 구현이 떨어진 가장 큰 한도] — 없거나 거꾸로면 None."""
    clean = [t.values[threshold] for t in level if t.clean]
    caught = [t.values[threshold] for t in level if t.caught]
    if not clean or not caught:
        return None
    lo, hi = min(clean), max(caught)
    return (lo, hi) if lo <= hi else None


def edge(level: Sequence[Trial], threshold: str) -> int | None:
    """창 끝을 다시 볼 한도 — 흔한 구현이 떨어진 가장 큰 한도 바로 위의 시험한 한도로, 허용 구현은 통과하고 흔한 구현은 통과했으며
    거기서 떨어지면 창이 MIN_RATIO 에 닿는 것. 흔한 구현이 한 번도 안 떨어졌거나, 위 한도에서 허용 구현이 떨어졌거나, 거기서
    떨어져도 창이 모자라면 None (다시 돌려도 판정이 안 바뀐다)."""
    caught = [t.values[threshold] for t in level if t.caught]
    clean = [t.values[threshold] for t in level if t.clean]
    if not caught or not clean:
        return None
    above = sorted({t.values[threshold] for t in level if t.values[threshold] > max(caught)})
    if not above:
        return None
    top = above[0]
    if not all(t.clean for t in level if t.values[threshold] == top):
        return None
    return top if top >= MIN_RATIO * min(clean) else None


def pick(lo: int, hi: int) -> int:
    """창의 기하 중간값 (STEP 단위로 반올림, 창 안으로)."""
    middle = round(math.sqrt(lo * hi) / STEP) * STEP
    return min(hi, max(lo, middle))


Runner = Callable[[dict[str, int]], Awaitable[Trial]]


async def search(
    k: Knobs, run: Runner, cases: Sequence[str] = (), limit: int = MAX_TRIALS
) -> Calibration:
    """부하 단계마다 한도 사다리를 내려가며 돌리고, 창이 MIN_RATIO 이상이면 그 단계의 기하 중간값을 고른다.
    창이 모자라면 창 끝 한도(edge)를 EDGE_RETRIES 번까지 더 돌리고, 고른 값은 CONFIRM 번 돌려 확인한다 (머리말 '시험 한 번은 흔들린다')."""
    trials: list[Trial] = []
    unconfirmed: list[int] = []

    async def attempt(values: dict[str, int]) -> Trial | None:
        if len(trials) >= limit:
            return None
        trial = await run(values)
        trials.append(trial)
        return trial

    for load in k.loads or (None,):
        fixed = {k.load: load} if k.load is not None and load is not None else {}
        level: list[Trial] = []
        for value in k.ladder:
            trial = await attempt({k.threshold: value, **fixed})
            if trial is None:
                break
            level.append(trial)
            if not trial.clean:
                break  # 한도가 짧을수록 허용 구현에 불리 — 그 아래는 보지 않는다
        if level and not level[0].clean:
            # 사다리 맨 위에서 허용 구현이 떨어짐 — 버스트 부하가 무겁다. 더 느슨한 한도로 올려 본다 (#337 7절). 흔한 구현이 계속 떨어지면
            # 창을 넓히려고 한 칸 더, 허용 구현이 통과하고 흔한 구현도 통과하면 거기가 창 위 끝이라 멈춘다
            for value in k.looser:
                trial = await attempt({k.threshold: value, **fixed})
                if trial is None:
                    break
                level.append(trial)
                if trial.clean and not trial.caught:
                    break
        w = window(level, k.threshold)
        top = edge(level, k.threshold) if w is None or w[1] < MIN_RATIO * w[0] else None
        if top is not None:
            for _ in range(EDGE_RETRIES):
                trial = await attempt({k.threshold: top, **fixed})
                if trial is None:
                    break
                level.append(trial)
                if trial.caught or not trial.clean:
                    break
            if not all(t.clean for t in level if t.values[k.threshold] == top):
                continue  # 창 끝에서 허용 구현이 흔들림 — 이 부하 단계에서는 고르지 않는다
            w = window(level, k.threshold)
        if w is None or w[1] < MIN_RATIO * w[0]:
            continue
        values = {k.threshold: pick(*w), **fixed}
        confirms: list[Trial] = []
        for _ in range(CONFIRM):
            trial = await attempt(values)
            if trial is None or not (trial.caught and trial.clean):
                unconfirmed.append(values[k.threshold])
                break
            confirms.append(trial)
        else:
            return Calibration(
                "calibrated",
                k,
                values,
                w,
                tuple(trials),
                tuple(cases),
                unconfirmed=tuple(unconfirmed),
                missed=_missed(cases, confirms),
            )
    status: Status
    if trials and not any(t.clean for t in trials):
        status = "overloaded"  # 더 느슨한 한도까지 허용 구현이 떨어짐 (#337 7절)
    elif any(t.caught for t in trials):
        status = "no_window"
    else:
        status = "naive_untouched"
    return Calibration(
        status, k, {}, None, tuple(trials), tuple(cases), unconfirmed=tuple(unconfirmed)
    )


def base_label(target: str) -> str:
    """보정 실행 대상 라벨(cal<n>-d1) → 원래 라벨(d1)."""
    return re.sub(rf"^{LABEL}\d+-", "", target)


def _missed(cases: Sequence[str], confirms: Sequence[Trial]) -> tuple[str, ...]:
    """고른 값의 확인 시험에서 흔한 구현이 한 번도 안 떨어진 케이스 — 시험이 케이스를 적지 않았으면(가짜 시험) 알 수 없어 비운다."""
    if not confirms or any(not t.caught_cases for t in confirms):
        return ()
    hit = set.intersection(*(set(t.caught_cases) for t in confirms))
    return tuple(c for c in cases if c not in hit)


def accepted_clean(report: VerifyReport) -> bool:
    """허용 구현이 하나 이상이고 모두 기동해 모든 테스트를 통과했나."""
    accepted = [r for r in report.runs if r.kind == "ref_accepted"]
    return bool(accepted) and all(
        r.setup == "ok" and bool(r.tests) and all(t.result == "PASS" for t in r.tests)
        for r in accepted
    )


def judge(
    report: VerifyReport,
    values: Mapping[str, int],
    request: Sequence[tuple[str, str]],
    intended: Mapping[str, str],
    family: Collection[str] = (),
) -> Trial:
    """조합 하나의 보고서 → 흔한 구현이 요청한 (라벨, 케이스) 중 어디서 의도한 사유로 떨어졌나 · 허용 구현이 모두 통과했나.
    케이스마다 따로 본다 (#337 7절) — 하나라도 떨어지면 caught, 떨어진 케이스는 caught_cases.
    family: 재료가 정한 이 숨은 요구의 사유들 — 같은 묶음의 다른 사유로 떨어져도 잡은 것으로 본다 (repair._gate3 와 같다)."""
    fam = set(family)
    runs = {base_label(r.target): r for r in report.runs}

    def caught(target: str, case: str) -> bool:
        run = runs.get(target)
        if run is None or run.setup != "ok":
            return False
        t = next((x for x in run.tests if x.group == "hidden" and x.name == case), None)
        want = intended.get(case)
        if t is None or t.result != "ASSERT" or want is None:
            return False
        return t.reason == want or (t.reason is not None and t.reason in fam and want in fam)

    hit = tuple(dict.fromkeys(c for t, c in request if caught(t, c)))
    return Trial(dict(values), bool(hit), accepted_clean(report), hit)


def with_params(hidden: Overlay, plan: HiddenPlan, m: TemplateManifest) -> Overlay:
    """숨은 오버레이의 조절값 파일만 그 계획에서 다시 쓴다 (generate 가 숨은 테스트에 붙인 것과 같은 render_params) —
    빌드가 조절값 파일을 계획과 대조하므로(static_check '조절값 파일을 손으로 씀') 파일과 계획은 늘 함께 바꾼다."""
    params = render_params(plan, m)
    files = tuple(params if f.path == params.path else f for f in hidden.files)
    if all(f.path != params.path for f in hidden.files):
        files = (*files, params)
    return replace(hidden, files=files)


# 검증기의 변형 실행 (starter/verify 의 verify_variants) — 시작 코드 + 대상 + 공개 테스트 + 숨은 테스트, 시작 코드 단독 실행 없이
VariantRunner = Callable[[Overlay, Sequence[Overlay], Overlay, Overlay], Awaitable[VerifyReport]]


async def calibrate_refs(
    run: VariantRunner,
    starter: Overlay,
    refs: Sequence[Overlay],
    public: Overlay,
    hidden: Overlay,
    plan: HiddenPlan,
    m: TemplateManifest,
    k: Knobs,
    request: Sequence[tuple[str, str]],
    family: Collection[str] = (),
    reports: list[VerifyReport] | None = None,
) -> Calibration:
    """참조 구현(흔한·허용)을 조합마다 다시 돌려 창을 찾는다 — 빌드(build._calibrate)와 오프라인 재생(eval/starter/replay_calibration)이
    같은 절차를 쓴다. 조합마다 숨은 오버레이의 조절값 파일만 바꾸고, 대상 라벨은 cal<n>- 를 붙인다. reports 에 실행 보고서를 모은다."""
    intended = {c.case_id: c.intended_reason for c in plan.cases}
    count = 0

    async def trial(values: dict[str, int]) -> Trial:
        nonlocal count
        count += 1
        tried = plan.model_copy(update={"harness": {**plan.harness, **values}})
        tagged = [replace(o, label=f"{LABEL}{count}-{o.label or o.kind}") for o in refs]
        report = await run(starter, tagged, public, with_params(hidden, tried, m))
        if reports is not None:
            reports.append(report)
        return judge(report, values, request, intended, family)

    return await search(k, trial, list(dict.fromkeys(c for _, c in request)))
