"""공개 테스트 변별 관문 — 규칙을 어긴 구현이 공개 테스트에서 떨어지는가 (#164, docs/design/spec-to-code.md §7).

숨은 테스트에는 흔한 구현이 떨어져야 하는 관문(③)이 있지만 공개 테스트는 "정답 구현이 통과하는가"만 봤다. 그래서 확인 방법이
한 점·느슨한 비교(남은 20·요청 30 만, 조회 결과 1건, 건수 ≤ 50)여도 아무것도 막지 않았고, 프롬프트 지침(plan_contract v26)만으로는
qwen 이 4건 중 1건만 지켰다 (2026-11-13). 이 관문은 경우(경계·정렬·건수·거르기·값)를 나열하지 않고 원칙 자체를 잰다:

- 공개 요구(r*)마다 정답 구현(허용 구현 d1)에서 그 요구 하나만 어긴 변형을 만든다 — 갈래는 빼먹기·뒤집기·경계 (`gen_variant`).
  공개 수용 기준(p*)이 아니라 요구 단위인 이유: 명세의 공개 수용 기준은 예시 한 점("남은 20 에 30 → 거절")이고 규칙(경계 — "적으면 거절",
  같으면 성공)은 요구에 있다. 기준 단위로 부르니 생성기가 "경계를 박지 않았다"며 경계 변형을 만들지 않았다 (2026-11-13 쿠폰 알파).
  비교 규칙(이상·이하·보다 적으면·넘으면·같으면·0이면·최대·한도…)이 있는 요구에 경계 변형이 없으면 한 번 더 부른다.
  변형을 만드는 쪽은 공개 테스트·확인 방법을 보지 않는다 (테스트를 쓴 쪽의 맹점을 모르는 독립 검사 — 2026-11-13 사용자).
- 변형마다 시작 코드 + 변형 + 공개 테스트를 돌린다 (숨은 테스트 없음). 공개 테스트 하나라도 떨어지면 잡힘, 모두 통과하면 살아남음,
  컴파일·기동이 안 되면 무효(셈에서 뺌).
- 살아남은 변형은 그 설명을 붙여 공개 테스트를 다시 만들고, 그래도 살아남으면 승인자 표시 — 그 기준은 공개 테스트로 재지 못한다.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

from task_spec.schemas import GeneratedFile, GenVariantOutput, TaskContract, TaskItem, VerifyReport
from task_spec.schemas.build import VariantOp
from task_spec.starter.static_check import Overlay

MAX_CHANGED_LINES = 12  # 변형 하나가 정답 구현에서 바꾼 줄 상한 — 한 덩어리 안에서도 작게 (최소성)
LABEL = "var"

OP_TEXT: dict[VariantOp, str] = {
    "omit": "빼먹기 — 그 규칙을 하지 않는다 (검사·정렬·거르기·제한·기록·계산을 뺀다)",
    "invert": "뒤집기 — 반대로 한다 (비교 방향·정렬 방향·더하기/빼기·참/거짓을 바꾼다)",
    "boundary": "경계 — 경계를 한 칸 어긋나게 한다 (`>=`↔`>`·`<=`↔`<`·기본값·상한 ±1·포함↔제외). "
    "요구·계약에 비교(이상·이하·초과·미만·보다 적으면/많으면·넘으면·같으면·0이면·최대·한도)가 있으면 반드시 만든다. "
    "'이후·이전'처럼 같은 값의 포함 여부를 정하지 않은 표현만 있으면 만들지 않는다",
}
OP_NAME: dict[VariantOp, str] = {"omit": "빼먹기", "invert": "뒤집기", "boundary": "경계"}

# 테스트 파일 — 변형은 앱 코드만 바꾼다 (테스트를 고쳐 통과하면 변형이 아니다)
_TEST_PATH = re.compile(
    r"(^|/)(src/test|tests?|__tests__)/|\.test\.[jt]sx?$|\.spec\.[jt]sx?$|(^|/)test_[^/]*\.py$|_test\.py$"
)


# 바꾸면 안 되는 파일 — 시드·스키마·설정·빌드 파일 (데이터가 바뀌면 규칙을 어긴 것이 아니라 다른 과제가 된다)
_FIXED_PATH = re.compile(r"\.(sql|ya?ml|properties|json|gradle|kts|toml|cfg|ini|xml|lock)$")


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def is_fixed_path(path: str) -> bool:
    return bool(_FIXED_PATH.search(path))


# 같은 값의 포함 여부를 정하는 비교 — 이런 요구는 경계 변형이 있어야 변별을 잰다 ('이후·이전'은 포함 여부를 정하지 않아 뺌)
_BOUNDARY = re.compile(
    r"이상|이하|초과|미만|보다\s*(적|많|작|크)|넘(으면|는|을|지)|모자라|부족|같으면|0\s*이면|최대|최소|상한|하한|한도|>=|<=|≥|≤"
)


def needs_boundary(text: str) -> bool:
    return bool(_BOUNDARY.search(text))


@dataclass(frozen=True)
class VariantTarget:
    """어길 규칙 하나 — 빌드는 공개 요구(r*), 손 시범은 측정 요구. acs = 그 요구를 보는 공개 수용 기준(판정: 그 기준의 테스트가 떨어져야 잡힘).
    acs 가 비면 어느 공개 테스트가 떨어져도 잡힘으로 본다."""

    rule_id: str
    text: str
    acs: tuple[str, ...] = ()


def _refs(node: object, out: list[list[str]]) -> None:
    if isinstance(node, dict):
        refs = node.get("refs")
        if isinstance(refs, list):
            out.append([str(x) for x in refs])
        for v in node.values():
            _refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _refs(v, out)


def requirement_acs(contract: TaskContract) -> dict[str, set[str]]:
    """요구(r*) → 같은 계약 요소의 refs 에 함께 나온 공개 수용 기준(p*) — 요구와 기준을 잇는 칸이 따로 없어 계약 요소로 잇는다."""
    out: dict[str, set[str]] = {}
    lists: list[list[str]] = []
    _refs(contract.model_dump(), lists)
    for refs in lists:
        ps = {x for x in refs if x.startswith("p")}
        for r in (x for x in refs if x.startswith("r")):
            out.setdefault(r, set()).update(ps)
    return out


def targets(task: TaskItem, contract: TaskContract) -> list[VariantTarget]:
    """공개 요구마다 — 그 요구 문장 + 이어진 공개 수용 기준 문장(예시). 이어진 기준이 없으면 공개 테스트가 있는 기준 모두로 (판정은 관대하게)."""
    tested = {t.ac_id for t in contract.public_tests}
    links = requirement_acs(contract)
    ac_text = {a.ac_id: a.text for a in task.acceptance_public}
    out: list[VariantTarget] = []
    for r in task.requirements:
        acs = sorted(links.get(r.req_id, set()) & tested)
        examples = " / ".join(f"{p} {ac_text[p]}" for p in acs if p in ac_text)
        text = r.text + (f" (공개 수용 기준 예: {examples})" if examples else "")
        out.append(VariantTarget(r.req_id, text, tuple(acs)))
    return out


def operators_text() -> str:
    return "\n".join(f"- `{op}` {text}" for op, text in OP_TEXT.items())


def app_files(*overlays: Overlay) -> dict[str, str]:
    """앞 오버레이 위에 뒤 오버레이를 덮은 앱 파일 (테스트 파일 빼고) — 정답 구현 = 시작 코드 + 허용 구현."""
    out: dict[str, str] = {}
    for o in overlays:
        for f in o.files:
            if not is_test_path(f.path):
                out[f.path] = f.content
    return out


def changed_hunks(before: str, after: str) -> int:
    """바뀐 곳 수 — 바뀌지 않은 줄로 갈린 덩어리 (연속한 줄을 고치면 1)."""
    ops = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    return sum(1 for tag, *_ in ops.get_opcodes() if tag != "equal")


_WORD = re.compile(r"\w+|\W")


def renamed(before: str, after: str) -> set[tuple[str, str]] | None:
    """바뀐 곳이 모두 '한 낱말을 다른 낱말로 바꾼 줄'이면 그 (전, 후) 낱말 쌍들, 아니면 None.

    이름 하나를 바꾸면 선언과 부르는 곳이 함께 바뀐다 — 2026-11-15 손 시범 retry-ae1005: 정렬이 Spring Data 파생 메서드 이름
    `findByMemberIdOrderByCreatedAtDesc` 에 있어 Asc 로 바꾸려면 저장소 선언과 호출 두 곳을 같이 고쳐야 했고, '한 곳만' 규칙에 매번
    버려져 정렬 요구(r2)를 잴 변형이 하나도 없었다 (빌드 관문 ⑩ 에서도)."""
    pairs: set[tuple[str, str]] = set()
    a, b = before.splitlines(), after.splitlines()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag != "replace" or i2 - i1 != j2 - j1:
            return None
        for old, new in zip(a[i1:i2], b[j1:j2], strict=True):
            x, y = _WORD.findall(old), _WORD.findall(new)
            diff = [(p, q) for p, q in zip(x, y, strict=False) if p != q]
            if (
                len(x) != len(y)
                or len(diff) != 1
                or not (diff[0][0].isidentifier() and diff[0][1].isidentifier())
            ):
                return None
            pairs.add(diff[0])
    return pairs


def changed_lines(before: str, after: str) -> int:
    """바꾼 줄 수 — 지운 줄과 더한 줄 중 많은 쪽 (한 줄 고침 = 1)."""
    minus = plus = 0
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        if line.startswith("-"):
            minus += 1
        elif line.startswith("+"):
            plus += 1
    return max(minus, plus)


@dataclass(frozen=True)
class VariantSpec:
    label: str
    rule_id: str
    acs: tuple[str, ...]
    op: VariantOp
    note: str
    paths: tuple[str, ...]  # 바꾼 파일
    changed: int  # 바꾼 줄 수


def build_variants(
    target: VariantTarget,
    out: GenVariantOutput,
    base: Overlay,
    impl: Mapping[str, str],
    taken: set[str] | None = None,
) -> tuple[list[tuple[Overlay, VariantSpec]], list[str]]:
    """생성 결과 → 실행할 변형 오버레이 (허용 구현 파일 + 바꾼 파일 — 시작 코드 위에 놓인다). 쓸 수 없는 변형은 이유와 함께 버린다:
    앱 파일이 아닌 경로·새 파일, 바뀐 것이 없음, 바꾼 줄이 상한을 넘음 (한 곳만 어기지 않음)."""
    taken = taken if taken is not None else set()
    rid = target.rule_id
    kept: list[tuple[Overlay, VariantSpec]] = []
    dropped: list[str] = []
    for v in out.variants:
        why: str | None = None
        changed = hunks = 0
        renames: set[tuple[str, str]] | None = set()
        for f in v.files:
            if f.path not in impl:
                why = f"정답 구현에 없는 파일 {f.path} (변형은 있는 앱 파일만 바꾼다)"
                break
            if is_fixed_path(f.path):
                why = f"시드·설정 파일 {f.path} 을 바꿈 (변형은 코드만 바꾼다)"
                break
            changed += changed_lines(impl[f.path], f.content)
            hunks += changed_hunks(impl[f.path], f.content)
            got = renamed(impl[f.path], f.content)
            renames = None if renames is None or got is None else renames | got
        if why is None and changed == 0:
            why = "정답 구현과 같음"
        # 같은 이름 하나를 선언·부르는 곳에서 함께 바꾼 것은 한 곳 — 낱말 쌍이 하나뿐일 때만 (서로 다른 곳을 고친 것과 가른다)
        if why is None and hunks > 1 and not (renames is not None and len(renames) == 1):
            # 1차 변이 — 한 곳만 어긴다. 여러 곳을 함께 바꾸면 한 곳의 확인에 걸려 다른 곳의 빈틈이 가려진다
            # (2026-11-13 N+1: '이름 null + 개수 0' 변형이 이름 확인에 잡혀 '개수만 0' 인 구현이 통과하는 빈틈을 못 봤다)
            why = f"바꾼 곳 {hunks}군데 (한 곳만 — 같은 규칙이 여러 곳에 있으면 곳마다 따로)"
        if why is None and changed > MAX_CHANGED_LINES:
            why = f"바꾼 줄 {changed} > {MAX_CHANGED_LINES} (한 곳만 어기지 않음)"
        if why is not None:
            dropped.append(f"{rid} {OP_NAME[v.op]}: {why}")
            continue
        label = f"{LABEL}-{rid}-{v.op}"
        n = 2
        while label in taken:
            label = f"{LABEL}-{rid}-{v.op}{n}"
            n += 1
        taken.add(label)
        mine = {f.path: f.content for f in v.files}
        files = [
            GeneratedFile(path=f.path, content=mine.pop(f.path, f.content)) for f in base.files
        ]
        files += [
            GeneratedFile(path=p, content=c) for p, c in mine.items()
        ]  # 시작 코드 파일을 바꾼 것
        overlay = Overlay(kind="ref_variant", files=tuple(files), label=label)
        spec = VariantSpec(
            label, rid, target.acs, v.op, v.note, tuple(f.path for f in v.files), changed
        )
        kept.append((overlay, spec))
    return kept, dropped


Verdict = Literal["caught", "survived", "invalid"]


@dataclass(frozen=True)
class VariantResult:
    label: str
    rule_id: str
    op: VariantOp
    note: str
    verdict: Verdict
    failed: tuple[str, ...] = ()  # 떨어진 공개 테스트 (잡힘일 때)
    detail: str = ""  # 무효 이유

    def line(self) -> str:
        head = f"{self.rule_id} {OP_NAME[self.op]} — {self.note}"
        if self.verdict == "caught":
            return f"{head}: 잡힘 ({', '.join(self.failed[:3])})"
        if self.verdict == "invalid":
            return f"{head}: 무효 ({self.detail})"
        return f"{head}: 살아남음 (공개 테스트 모두 통과)"


def verdict_of(
    setup: str, ran_public: bool, failed: Sequence[str], own: Sequence[str], setup_detail: str = ""
) -> tuple[Verdict, str]:
    """변형 하나의 판정과 무효 이유 — 관문 ⑩(`judge`)과 손 시범 유효성(`eval/pilot/validity.py`)이 같은 정의를 쓴다 (#151).
    컴파일·기동 실패·결과 없음은 무효, 공개 테스트가 모두 통과하면 살아남음, 겨냥한 요구(기준)의 테스트(own)가 떨어지면 잡힘,
    다른 요구의 테스트만 떨어지면 무효 — 한 곳만 어기지 않은 변형이라 그 요구의 변별 근거가 아니다."""
    if setup != "ok":
        return "invalid", setup + (f": {setup_detail[:120]}" if setup_detail else "")
    if not ran_public:
        return "invalid", "공개 테스트 결과 없음"
    if not failed:
        return "survived", ""
    if own:
        return "caught", ""
    return (
        "invalid",
        f"다른 기준의 공개 테스트만 떨어짐 ({', '.join(failed[:3])}) — 한 곳만 어기지 않음",
    )


_PT = re.compile(r"\bpt(\d{1,2})\b", re.IGNORECASE)


def pts_by_test(public: Overlay) -> dict[str, set[str]]:
    """공개 테스트 결과 이름(클래스·파일) → 그 테스트가 보는 pt id — 파일 본문의 pt id(@DisplayName·describe 제목)로.
    변형이 겨냥한 기준의 테스트가 떨어졌는지 가르려고 (리뷰 2026-11-13: 다른 기준의 테스트만 떨어져도 잡힘으로 셌다)."""
    out: dict[str, set[str]] = {}
    for f in public.files:
        pts = {f"pt{int(n)}" for n in _PT.findall(f.content)}
        if not pts:
            continue
        path = PurePosixPath(f.path)
        for key in {path.stem, path.name, f.path, *re.findall(r"\bclass\s+(\w+)", f.content)}:
            out.setdefault(key, set()).update(pts)
    return out


def _pts_of(name: str, pts: Mapping[str, set[str]]) -> set[str]:
    if name in pts:
        return pts[name]
    return {f"pt{int(n)}" for n in _PT.findall(name)}


def judge(
    report: VerifyReport,
    specs: Sequence[VariantSpec],
    pts: Mapping[str, set[str]] | None = None,
    pt_ac: Mapping[str, str] | None = None,
) -> list[VariantResult]:
    """변형마다 — 겨냥한 요구에 이어진 기준(spec.acs)의 공개 테스트가 떨어지면 잡힘, 공개 테스트가 모두 통과하면 살아남음, 컴파일·기동 실패·결과 없음은 무효.
    다른 기준의 테스트(또는 pt 가 없는 저장 확인 등)만 떨어지면 무효 — 한 곳만 어기지 않은 변형이라 그 기준의 변별 근거가 아니다.
    pt 대응(pts·pt_ac)이 없으면(결과 이름으로 pt 를 못 찾는 스택) 어느 공개 테스트가 떨어져도 잡힘으로 본다."""
    by_label = {r.target: r for r in report.runs}
    known = bool(pts) and bool(pt_ac)
    out: list[VariantResult] = []
    for s in specs:
        run = by_label.get(s.label)

        def bad(detail: str, spec: VariantSpec = s) -> VariantResult:
            return VariantResult(
                spec.label, spec.rule_id, spec.op, spec.note, "invalid", detail=detail
            )

        if run is None:
            out.append(bad("실행 없음"))
            continue
        public = run.results("public")
        failed = [t.name for t in public if t.result != "PASS"]
        acs = pt_ac or {}
        mine = set(s.acs)
        own = (
            [n for n in failed if mine & {acs[p] for p in _pts_of(n, pts or {}) if p in acs}]
            if known and mine
            else failed
        )
        verdict, detail = verdict_of(run.setup, bool(public), failed, own, run.setup_detail)
        if verdict == "invalid":
            out.append(bad(detail))
        else:
            out.append(
                VariantResult(
                    s.label,
                    s.rule_id,
                    s.op,
                    s.note,
                    verdict,
                    failed=tuple(own) if verdict == "caught" else (),
                )
            )
    return out


def survivors(results: Sequence[VariantResult]) -> list[VariantResult]:
    return [r for r in results if r.verdict == "survived"]


def repair_note(
    results: Sequence[VariantResult],
    test_ids: Mapping[str, Sequence[str]],
    rule_acs: Mapping[str, Sequence[str]] | None = None,
    *,
    timed: bool = False,
) -> str:
    """살아남은 변형 → 공개 테스트 수리 요청. 변형의 코드는 주지 않는다 — 그 구현 하나를 겨냥하는 테스트가 아니라
    그 기준을 어긴 구현 일반이 떨어지는 데이터·단언을 쓰게 (경계 값과 바로 바깥 값, 정렬 키가 다른 3건 이상, 정확한 개수·값,
    걸러질 행이 결과에 나올 수 있는 조회).

    timed: 도우미에 시간 규칙 함수가 있다 (Java + 계약에 초 단위 설정, hidden_helpers._time_rule_lines). 2026-11-25 #308 r3: 연장을
    빼먹은 변형이 살아남자 재생성이 다시 0.1초 여유로 단언하는 테스트를 더했다 — 시간 규칙은 그 함수로 재라고 가리킨다."""

    def where(rule: str) -> str:
        acs = list((rule_acs or {}).get(rule, ()))
        pts = [t for a in acs for t in test_ids.get(a, ())]
        return f"{rule}({', '.join(pts)})" if pts else rule

    lines = [
        f"- {where(r.rule_id)}: '{r.note}' ({OP_NAME[r.op]}) 인 구현이 공개 테스트를 모두 통과했다"
        for r in results
    ]
    return (
        "규칙을 어긴 구현이 공개 테스트를 통과한다 — 기존 공개 테스트 파일은 그대로 두고, 아래 요구를 보는 pt 마다 "
        "그런 구현에서 떨어지는 새 테스트 클래스를 더한다. "
        "그 구현 하나만 겨냥하지 말고, 규칙을 지킨 구현과 어긴 구현이 다른 결과를 내는 데이터와 단언으로 "
        "(경계는 경계 값과 바로 바깥 값 쌍, 정렬은 정렬 키가 다른 3건 이상의 순서, 건수·계산 값은 정확히, "
        "거르기는 걸러질 행이 결과에 나올 수 있는 조회에서 그 행이 없음). 데이터는 도우미 상수의 행이나 요청으로 만든다. "
        "기존 테스트는 고치지 않는다:\n"
        + "\n".join(lines)
        + (
            "\n시간이 지나야 드러나는 규칙(연장·만료)은 도우미의 assertRenewedWhileUsed·beforeExpiry·afterExpiry 로 잰다 — "
            "만료를 정수 초로 다루는 구현은 최대 1초 늦게 만료하므로 시간 값 ±1초 안에서 Thread.sleep 으로 재지 않는다"
            if timed
            else ""
        )
    )


@dataclass
class VariantGate:
    """빌드 하나의 관문 상태 — 변형은 허용 구현이 바뀔 때만 다시 만든다 (공개 테스트만 바뀌면 같은 변형을 다시 돌림)."""

    base_digest: str = ""
    variants: list[tuple[Overlay, VariantSpec]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    results: list[VariantResult] = field(default_factory=list)
    missing_boundary: list[str] = field(default_factory=list)
    rounds: int = 0  # 변형을 돌린 횟수
