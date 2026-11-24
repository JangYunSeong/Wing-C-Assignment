"""재료 손 구현 관문 (⑫, #256 3단계) — 재료당 한 번 써 둔 손 구현을 모든 빌드의 공개·숨은 테스트에 돌린다 (LLM 호출 없음).

11-06 손 예시(daily-agg-pipeline REPORT §3)의 검증 관문 ③·③'·④·⑤ 를 빌드 안에서 그대로 확인한다.
- H④ 허용 손 구현 × 공개·숨은 테스트 — 모두 통과. 허용 손 구현이 모두 떨어지는 테스트는 결함 → 그 테스트를 다시 만든다.
  일부만 떨어지면 표시만 (예: 최근 N일 설계가 N일보다 늦은 레코드에서 — 재료 설계의 조건).
- H⑤ 흔한·하나만 놓친 손 구현 × 공개 테스트 — 모두 통과. 떨어지면 공개 테스트가 숨은 요구를 시험한다(누설) → 공개 테스트를 다시.
- H③ 하나만 놓친 손 구현은 그 사유 케이스에서, 흔한 손 구현은 숨은 케이스 하나 이상에서 떨어진다. 아니면 숨은 테스트를 다시.
- H③' 하나만 놓친 손 구현이 다른 사유 케이스에서도 떨어지면 표시만 (케이스가 사유를 섞음).
- 구하기: 생성 흔한 구현이 통과해 '변별 못 함'인 케이스도 그 사유만 놓친 손 구현이 떨어뜨리거나 흔한 손 구현이 의도한 사유 태그로
  떨어뜨리면 채점에 남긴다.

흔한·하나만 놓친 손 구현의 판정은 허용 손 구현이 모두 통과한 숨은 케이스만 본다 (결함 케이스에서 떨어진 것은 근거가 아니다).
결과가 없는 테스트는 근거로 쓰지 않고 표시만 한다 (수집 오류·환경 문제로 LLM 재생성을 부르지 않게). 허용 손 구현이 모든 테스트에서
떨어지면 테스트 결함이 아니라 손 구현이 이 계약·채점 환경에서 돌지 않는 것이라 판정하지 않는다.
손 구현은 손 도우미(공용 폴더)를 함께 얹으므로 시작 코드 자기 테스트(existing)는 보지 않는다. 수리·되돌리기는 build._hand_gate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

import yaml

from task_spec.schemas import GeneratedFile, MaterialInterface, ReferenceManifest, VerifyReport
from task_spec.schemas.build import HiddenPlan
from task_spec.schemas.incident import HandReference
from task_spec.schemas.verify_report import TargetRun, TestResult
from task_spec.starter.interface import interface_ref
from task_spec.starter.static_check import Overlay

GATE = "⑫"
NAME = f"손 구현 관문({GATE})"
LABEL = "hand-"  # 검증 대상 라벨 접두사 — 생성 참조 구현 라벨(naive-all·d1)과 겹치지 않게
MANIFEST = "references.yaml"
MAX_MESSAGE = 160

_KIND_NAME = {"accepted": "허용", "naive": "흔한 구현", "one_miss": "하나만 놓침"}


@dataclass(frozen=True)
class HandReferences:
    """재료 하나의 손 구현 묶음 — 매니페스트와 손 구현마다 얹을 파일 (공용 폴더 + 자기 폴더, 같은 경로는 자기 것)."""

    manifest: ReferenceManifest
    files: Mapping[str, tuple[GeneratedFile, ...]]
    folder: str = ""
    stale: str = ""  # 관문을 끈 이유 — 비어 있으면 돈다 (checked_against)

    @property
    def refs(self) -> list[HandReference]:
        return list(self.manifest.refs)

    def overlays(self) -> list[Overlay]:
        return [
            Overlay(
                kind="ref_accepted" if r.kind == "accepted" else "ref_naive",
                files=self.files[r.name],
                label=LABEL + r.name,
            )
            for r in self.manifest.refs
        ]

    def checked_against(self, interface: MaterialInterface | None) -> HandReferences:
        """손 구현은 인터페이스 이름·위치·형식대로 실행된다 — 확인한 판과 지금 인터페이스가 다르면 관문을 끈다."""
        if interface is None:
            return replace(self, stale="재료 인터페이스가 없음 (손 구현은 인터페이스대로 실행된다)")
        if interface_ref(interface) != self.manifest.interface_sha256:
            return replace(
                self,
                stale="손 구현을 확인한 인터페이스 판과 지금 인터페이스가 다름 — 손 구현을 다시 확인하고 "
                f"references/{self.folder}/{MANIFEST} 의 지문을 고칠 것",
            )
        return replace(self, stale="")


def load_references(root: Path) -> dict[str, HandReferences]:
    """재료 id → 손 구현 묶음 (`<root>/references/*/references.yaml`, #256 3단계). 폴더가 없으면 빈 dict.
    같은 재료를 두 묶음이 정하거나 손 구현 폴더가 비어 있으면 거부한다."""
    out: dict[str, HandReferences] = {}
    base = root / "references"
    if not base.is_dir():
        return out
    for mf in sorted(base.glob(f"*/{MANIFEST}")):
        folder = mf.parent
        manifest = ReferenceManifest.model_validate(yaml.safe_load(mf.read_text("utf-8")))
        if manifest.incident_id in out:
            raise ValueError(
                f"재료 {manifest.incident_id} 의 손 구현 묶음이 둘 이상입니다: {folder.name}"
            )
        shared = {f.path: f for f in _files(folder / manifest.shared)}
        files: dict[str, tuple[GeneratedFile, ...]] = {}
        for r in manifest.refs:
            own = _files(folder / r.name)
            if not own:
                raise ValueError(f"{folder.name}: 손 구현 {r.name} 의 파일이 없음")
            files[r.name] = tuple({**shared, **{f.path: f for f in own}}.values())
        out[manifest.incident_id] = HandReferences(manifest, files, folder.name)
    return out


def _files(d: Path) -> list[GeneratedFile]:
    if not d.is_dir():
        return []
    return [
        GeneratedFile(path=p.relative_to(d).as_posix(), content=p.read_text("utf-8"))
        for p in sorted(d.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    ]


Rule = Literal["H4", "H5", "H3"]


@dataclass(frozen=True)
class HandFinding:
    """수리할 것 하나 — 생성기마다 한 요청으로 합친다 (build._hand_gate)."""

    rule: Rule
    generator: Literal["gen_hidden", "gen_public"]
    test: str  # 공개 테스트 이름 · 숨은 케이스 id · '' (숨은 테스트 전체)
    refs: tuple[str, ...]  # 짚은 손 구현 이름
    message: str  # 실패 결과·메시지 앞부분 (손 구현 이름·설명 없이 — 공개 테스트 수리 요청에 그대로 나간다)
    detail: str = ""  # 손 구현 설명을 담은 한 줄 — 숨은 테스트 수리 요청·승인 화면에만
    cases: tuple[str, ...] = ()  # 겨냥한 숨은 케이스


@dataclass
class HandJudgement:
    findings: list[HandFinding] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # 표시만 (막지 않음)
    keep: frozenset[str] = frozenset()  # 손 구현이 의도한 사유로 떨어뜨리는 숨은 케이스 (구하기)
    lines: list[str] = field(default_factory=list)  # 손 구현마다 결과 한 줄 — 보고서


@dataclass
class HandGate:
    """빌드 하나의 손 구현 관문 상태 — 마지막 판정·실행 횟수."""

    findings: list[HandFinding] = field(default_factory=list)
    keep: frozenset[str] = frozenset()
    rounds: int = 0
    off_noted: bool = False


def _result(run: TargetRun, group: str, name: str) -> TestResult | None:
    return next((t for t in run.tests if t.group == group and t.name == name), None)


def _failed(run: TargetRun, group: str, name: str) -> TestResult | None:
    """그 테스트에서 떨어졌으면 결과 — 결과가 없으면 None (근거로 쓰지 않는다 — judge_hands 가 따로 표시)."""
    t = _result(run, group, name)
    return t if t is not None and t.result != "PASS" else None


def _passed(run: TargetRun, group: str, name: str) -> bool:
    t = _result(run, group, name)
    return t is not None and t.result == "PASS"


def _msg(t: TestResult) -> str:
    text = " ".join(t.message.split())[:MAX_MESSAGE]
    return " ".join(x for x in (t.result, f"[{t.reason}]" if t.reason else "", text) if x)


def _names(rs: Sequence[tuple[HandReference, object]]) -> str:
    return ", ".join(r.name for r, _ in rs)


def judge_hands(report: VerifyReport, refs: HandReferences, plan: HiddenPlan) -> HandJudgement:
    """손 구현 실행 결과 → 수리할 것·표시·구하기. 숨은 케이스는 계획 순서로, 공개 테스트는 이름 순서로 본다."""
    j = HandJudgement()
    runs: list[tuple[HandReference, TargetRun]] = []
    for r in refs.refs:
        run = report.run(LABEL + r.name)
        setup = run.setup if run is not None else "결과 없음"
        if (
            run is not None
            and setup == "ok"
            and not (run.results("public") or run.results("hidden"))
        ):
            setup = "테스트 결과 없음"
        if run is None or setup != "ok":
            j.flags.append(
                f"{NAME}: 손 구현 {r.name} 실행 준비 실패({setup}) — 판정에서 뺌. 사람 확인"
            )
            continue
        runs.append((r, run))
    j.lines = [_line(r, run) for r, run in runs]
    accepted = [(r, run) for r, run in runs if r.kind == "accepted"]
    naive_like = [(r, run) for r, run in runs if r.kind != "accepted"]
    if not accepted:
        j.flags.append(f"{NAME}: 실행된 허용 손 구현이 없어 판정하지 못함 — 사람 확인")
        return j

    public = sorted({t.name for _, run in runs for t in run.results("public")})
    seen = {t.name for _, run in runs for t in run.results("hidden")}
    cases = [c for c in plan.cases if c.case_id in seen]
    missing = [
        f"{r.name}:{name}"
        for r, run in runs
        for group, names in (("public", public), ("hidden", [c.case_id for c in cases]))
        for name in names
        if _result(run, group, name) is None
    ]
    if missing:
        j.flags.append(
            f"{NAME}: 결과 없는 테스트 {', '.join(missing[:6])} — 판정 근거에서 뺌 (테스트가 시작 코드 내부를 import 하는지 확인). 사람 확인"
        )
    if all(
        not any(t.result == "PASS" for t in [*run.results("public"), *run.results("hidden")])
        for _, run in accepted
    ):
        j.flags.append(
            f"{NAME}: 허용 손 구현이 모든 테스트에서 실패 — 손 구현이 이 계약·채점 환경에서 돌지 않음 (인터페이스·하네스 확인). "
            "판정하지 않음. 사람 확인"
        )
        return j

    # H④ — 허용 손 구현이 모두(결과가 있고) 떨어지면 테스트 결함, 일부만이면 표시
    defect_public: set[str] = set()
    for p in public:
        bad = [(r, t) for r, run in accepted if (t := _failed(run, "public", p)) is not None]
        complete = all(_result(run, "public", p) is not None for _, run in accepted)
        if bad and len(bad) == len(accepted) and complete:
            defect_public.add(p)
            j.findings.append(
                HandFinding("H4", "gen_public", p, tuple(r.name for r, _ in bad), _msg(bad[0][1]))
            )
        elif bad:
            j.flags.append(
                f"{NAME}: 허용 손 구현 {_names(bad)} 만 공개 테스트 {p} 에서 실패 — 한 구현 방식에 기댄 공개 테스트 의심 "
                f"({_msg(bad[0][1])}). 사람 확인"
            )
    sound: list[str] = []  # 허용 손 구현이 모두(결과가 있고) 통과한 숨은 케이스
    for c in cases:
        bad = [
            (r, t) for r, run in accepted if (t := _failed(run, "hidden", c.case_id)) is not None
        ]
        if not bad:
            if all(_passed(run, "hidden", c.case_id) for _, run in accepted):
                sound.append(c.case_id)
        elif len(bad) == len(accepted):
            j.findings.append(
                HandFinding(
                    "H4",
                    "gen_hidden",
                    c.case_id,
                    tuple(r.name for r, _ in bad),
                    _msg(bad[0][1]),
                    detail=f"허용 설계 구현이 모두 숨은 케이스 {c.case_id} 에서 실패 — {_msg(bad[0][1])}",
                    cases=(c.case_id,),
                )
            )
        else:
            j.flags.append(
                f"{NAME}: 허용 손 구현 {_names(bad)} 만 숨은 케이스 {c.case_id} 에서 실패 — 허용 설계의 조건을 넘는 케이스 의심 "
                f"({_msg(bad[0][1])}). 사람 확인"
            )

    # H⑤ — 흔한·하나만 놓친 손 구현은 공개 테스트를 모두 통과 (결함으로 본 공개 테스트는 빼고)
    for p in public:
        if p in defect_public:
            continue
        bad = [(r, t) for r, run in naive_like if (t := _failed(run, "public", p)) is not None]
        if bad:
            j.findings.append(
                HandFinding("H5", "gen_public", p, tuple(r.name for r, _ in bad), _msg(bad[0][1]))
            )

    # H③ · H③' — 허용 손 구현이 모두 통과한 케이스에서
    for r, run in naive_like:
        if r.kind == "one_miss":
            reason = r.misses[0]
            mine = [c.case_id for c in cases if c.intended_reason == reason]
            if not mine:
                j.flags.append(
                    f"{NAME}: 숨은 계획에 사유 {reason} 케이스가 없음 — 그 사유만 놓친 손 구현 {r.name} 을 시험하지 못함. 사람 확인"
                )
                continue
            judged = [cid for cid in mine if cid in sound and _result(run, "hidden", cid)]
            if judged and not any(_failed(run, "hidden", cid) for cid in judged):
                j.findings.append(
                    HandFinding(
                        "H3",
                        "gen_hidden",
                        ",".join(judged),
                        (r.name,),
                        "모두 통과",
                        detail=f"사유 {reason} 만 놓친 구현({r.note})이 그 사유 케이스 {', '.join(judged)} 를 모두 통과",
                        cases=tuple(judged),
                    )
                )
            mixed = [
                c.case_id
                for c in cases
                if c.intended_reason != reason
                and c.case_id in sound
                and _failed(run, "hidden", c.case_id)
            ]
            if mixed:
                j.flags.append(
                    f"{NAME}: 사유 {reason} 만 놓친 손 구현 {r.name} 이 다른 사유 케이스 {', '.join(mixed)} 에서도 실패 — "
                    "케이스가 여러 사유를 함께 시험함 의심. 사람 확인"
                )
        elif [cid for cid in sound if _result(run, "hidden", cid)] and not any(
            _failed(run, "hidden", cid) for cid in sound
        ):
            j.findings.append(
                HandFinding(
                    "H3",
                    "gen_hidden",
                    "",
                    (r.name,),
                    "모두 통과",
                    detail=f"흔한 구현({r.note})이 숨은 케이스를 모두 통과",
                )
            )

    # 구하기 — 허용 손 구현이 모두 통과하고, 그 사유만 놓친 손 구현이 떨어뜨리거나 흔한 손 구현이 의도한 사유 태그로 떨어뜨리는 케이스
    def catches(r: HandReference, run: TargetRun, c_id: str, reason: str) -> bool:
        t = _failed(run, "hidden", c_id)
        if t is None:
            return False
        return r.misses == [reason] if r.kind == "one_miss" else t.reason == reason

    j.keep = frozenset(
        c.case_id
        for c in cases
        if c.case_id in sound
        and any(catches(r, run, c.case_id, c.intended_reason) for r, run in naive_like)
    )
    return j


def _line(r: HandReference, run: TargetRun) -> str:
    kind = _KIND_NAME[r.kind] + (f" {r.misses[0]}" if r.kind == "one_miss" else "")
    pub = run.results("public")
    ok = sum(t.result == "PASS" for t in pub)
    hidden = " ".join(
        f"{t.name}:{'통과' if t.result == 'PASS' else (t.reason or t.result)}"
        for t in run.results("hidden")
    )
    return f"{r.name} ({kind}) 공개 {ok}/{len(pub)} · 숨은 {hidden or '-'}"


def hidden_note(findings: Sequence[HandFinding]) -> str:
    """숨은 테스트 수리 요청 — 손 구현이 짚은 케이스·결과. 손 구현 코드는 주지 않는다 (그 구현 하나가 아니라 그 모양 일반을 가르게)."""
    lines = [
        "재료의 손 구현(사람이 확인한 허용 설계 구현·흔한 구현·숨은 요구의 사유 하나만 놓친 구현)으로 숨은 테스트를 돌린 결과:"
    ]
    for f in findings:
        if f.rule == "H4":
            lines.append(
                f"- {f.detail}. 재료의 허용 설계는 모두 통과해야 한다 — 이 케이스가 한 구현 방식(파일 이름·쓰는 순서·중간 상태)에 "
                "기대거나 공개 계약 밖의 것을 단언하지 않는지 보고 고친다."
            )
        else:
            lines.append(
                f"- {f.detail}. 그 사유의 케이스가 이런 구현을 의도한 사유로 떨어뜨리게 입력·단언을 고친다 (다른 케이스는 그대로)."
            )
    return "\n".join(lines)


def public_note(findings: Sequence[HandFinding], rules: Sequence[str]) -> str:
    """공개 테스트 수리 요청 — 손 구현 이름·설명(숨은 요구를 담음)은 넣지 않는다. 인터페이스의 공개 테스트 입력 규칙을 붙인다."""
    lines = ["재료의 손 구현으로 공개 테스트를 돌린 결과:"]
    for f in findings:
        if f.rule == "H4":
            lines.append(
                f"- 공개 계약대로 짠 허용 설계 구현이 모두 공개 테스트 {f.test} 에서 실패: {f.message}. 공개 테스트를 고친다."
            )
        else:
            lines.append(
                f"- 공개 요구만 지킨 구현이 공개 테스트 {f.test} 에서 실패: {f.message}. 공개 테스트는 공개 요구만 시험한다 — "
                "입력·단언에서 공개 요구 밖의 장면을 뺀다."
            )
    lines.append("짚은 공개 테스트만 고친다 — 다른 공개 테스트 파일은 그대로 둔다.")
    if rules:
        lines.append("공개 테스트 입력 규칙 (재료):")
        lines += [f"- {r}" for r in rules]
    return "\n".join(lines)


def finding_flags(findings: Sequence[HandFinding], why: str) -> list[str]:
    """고치지 못하고 남은 것 — 승인 화면 표시 (막지 않는다)."""
    out: list[str] = []
    for f in findings:
        if f.rule == "H4":
            what = "공개 테스트" if f.generator == "gen_public" else "숨은 케이스"
            out.append(
                f"{NAME}: 허용 손 구현이 모두 {what} {f.test} 에서 실패 ({why}) — 테스트 결함 의심: {f.message}. 사람 확인"
            )
        elif f.rule == "H5":
            out.append(
                f"{NAME}: 흔한 손 구현 {', '.join(f.refs)} 가 공개 테스트 {f.test} 에서 실패 ({why}) — 공개 테스트가 숨은 요구를 "
                f"시험함 (누설 의심): {f.message}. 사람 확인"
            )
        else:
            out.append(
                f"{NAME}: {f.detail} ({why}) — 그 구현을 숨은 테스트로 가르지 못함. 사람 확인"
            )
    return out
