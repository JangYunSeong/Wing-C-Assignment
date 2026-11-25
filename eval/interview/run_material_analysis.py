"""재료 입구 분석 평가 (#301 PR 3, prompts/analyze_material) — 재료가 빌드 하네스에서 재현되는지 LLM 이 맞게 짚는가.

표본 (가상·공개만 — 지원자·회사 원문 없음):
- 공개 재료 4: 풀 교착(쥔 채 대기) · 갱신 손실 · 재시도 중복 · 목록 조회(N+1)
- 가상 재료 2: 세션 유지 포화형(요청마다 세션 테이블 조회·갱신 — #301 의 발견 사례를 일반화) · 재현 불가형(로그 디스크가 가득 참 —
  하네스가 디스크 상태를 바꿀 수 없다)
기대(실행 전 고정): 사유는 기대 목록 안, 갈림 조절값은 기대 집합을 모두 포함, 재현 가능 여부는 기대 목록 안. 재현 불가형은 표시가 나와야 하고
나머지는 표시가 없어야 한다 (interview/material_analysis.analysis_flags).

평가할 판을 CURRENT 로 둔 임시 프롬프트 폴더를 쓴다 (레포의 CURRENT 는 건드리지 않는다). 보정 칸(tunes)은 --template-version 판에서
(기본 4). 재료 블록은 운영과 같은 저장본 검사(stored_json)를 거친다. 결과는 git 밖에 쓴다.

사용:
  uv run python eval/interview/run_material_analysis.py --version 1 --runs 2 --out data/eval/interview/material_analysis
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from task_spec.config import Settings
from task_spec.interview.material_analysis import (
    TASK,
    analysis_flags,
    analyze_material,
    material_block,
)
from task_spec.llm.factory import build_llm_client
from task_spec.nodes.pseudonymize import InterviewPseudonymizer
from task_spec.nodes.pseudonymize.dictionaries import load_dictionaries
from task_spec.nodes.pseudonymize.engine import Detectors, RunMapping
from task_spec.prompting import PromptRegistry
from task_spec.schemas import PublicIncident
from task_spec.spec.incidents import incident_by_id, load_incidents
from task_spec.starter.templates import load_template

REPO = Path(__file__).resolve().parents[2]
JAVA = "backend/java-spring-boot3-jpa"
DEADLOCK = "pub:sw.backend:pool-hold-and-wait-deadlock"


@dataclass(frozen=True)
class Sample:
    name: str
    incident: PublicIncident
    reasons: tuple[str, ...]  # 맞다고 볼 사유
    params: frozenset[str]  # 갈림 조절값에 모두 들어 있어야 할 것
    reproducible: tuple[str, ...]  # 맞다고 볼 재현 가능 여부
    flagged: bool = False  # 표시가 나와야 하나


def _virtual(base: PublicIncident, incident_id: str, **fields: Any) -> PublicIncident:
    """공개 재료를 바탕으로 기전 칸만 바꾼 가상 재료 — model_copy 는 검증하지 않으니 칸 이름만 맞춘다."""
    feature = base.feature
    assert feature is not None
    feature_fields = {k: fields.pop(k) for k in list(fields) if k in type(feature).model_fields}
    return base.model_copy(
        update={
            "incident_id": incident_id,
            "title": "가상 재료",
            "org": "",
            "url": "",
            "feature": feature.model_copy(update=feature_fields),
            **fields,
        }
    )


def samples(root: Path) -> list[Sample]:
    catalog = load_incidents(root)

    def pub(incident_id: str) -> PublicIncident:
        found = incident_by_id(catalog, incident_id)
        assert found is not None, incident_id
        return found

    base = pub(DEADLOCK)
    session = _virtual(
        base,
        "co:sw.backend:virtual-session",
        symptom="피크 시간대 로그인 유지 요청이 늘면 일부 요청이 DB 연결 획득 시간 초과로 실패",
        root_cause="요청마다 세션 테이블을 조회하고 마지막 접근 시각을 갱신해 요청 하나가 DB 문장을 여러 개 실행한다 — 쥔 채 기다리지는 않지만 연결을 쥐는 시간이 길어 대기가 쌓인다",
        naive_fix="커넥션 풀 크기를 늘린다",
        why_naive_fails="부하가 바뀌면 다시 대기가 쌓인다 — 요청당 DB 일을 줄이지 않았다",
        notes="커넥션 풀 수치·동시 접속 규모는 확인되지 않음",
        naive_impl="요청마다 세션 테이블에서 세션을 조회하고 마지막 접근 시각을 갱신한 뒤 본 처리를 한다",
        trigger="피크 시간대 동시 요청 수가 커넥션 풀 크기보다 크다",
        why_naive_breaks="요청 하나가 DB 문장 약 4개를 실행해 연결을 오래 쥐고, 동시 요청이 풀보다 많으면 뒤 요청의 연결 대기가 대기 한도를 넘는다",
        hidden_requirement="피크 시간대에도 로그인 유지 확인이 DB 연결 대기로 실패하지 않는다",
        accepted_designs=[
            "[설계] 서명된 토큰으로 세션을 확인해 요청마다 DB 를 보지 않음 — 조건: 만료·폐기 확인 경로가 있음",
            "[설계] 세션을 메모리 캐시에 두고 일정 간격으로만 DB 에 반영 — 조건: 캐시 만료가 세션 만료보다 짧음",
        ],
        reasons=["pool_acquire_timeout"],
        harness=["pool_size", "concurrency"],
    )
    disk = _virtual(
        base,
        "co:sw.backend:virtual-disk-full",
        symptom="야간 배치 뒤 로그 디스크가 가득 차 API 가 요청 기록을 쓰지 못하고 500 을 돌려줌",
        root_cause="요청 로그 파일을 회전하지 않아 디스크가 가득 찼다",
        naive_fix="로그 수준을 낮춘다",
        why_naive_fails="쌓이는 속도만 늦출 뿐 디스크가 차는 조건은 그대로다",
        notes="디스크 크기·로그 증가 속도는 확인되지 않음",
        naive_impl="요청마다 로컬 파일에 요청 기록 한 줄을 덧붙인다",
        trigger="디스크 여유 공간이 바닥난다",
        why_naive_breaks="파일 쓰기가 실패하면 요청 처리 전체가 실패한다",
        hidden_requirement="로그 디스크가 가득 차도 요청 처리는 성공하고 기록 실패만 따로 알린다",
        accepted_designs=[
            "[설계] 로그 쓰기 실패를 요청 처리와 떼어 내고 경보만 남김 — 조건: 기록 누락을 다른 경로로 셀 수 있음",
            "[설계] 크기 기준으로 로그 파일을 회전 — 조건: 보관 기간이 디스크 크기 안에 맞음",
        ],
        reasons=["server_error"],
        harness=[],
    )
    return [
        Sample(
            "pool-deadlock",
            base,
            ("pool_acquire_timeout",),
            frozenset({"pool_size", "concurrency"}),
            ("yes",),
        ),
        Sample(
            "lost-update",
            pub("pub:sw.backend:read-modify-write-lost-update"),
            ("lost_update",),
            frozenset({"concurrency"}),
            ("yes",),
        ),
        Sample(
            "retry-duplicate",
            pub("pub:sw.backend:retry-double-processing"),
            ("duplicate_record", "record_mismatch"),
            frozenset(),
            ("yes",),
        ),
        Sample(
            "list-n-plus-one",
            pub("pub:sw.backend:list-query-per-row"),
            ("request_timeout",),
            frozenset({"query_delay_ms", "request_timeout_s"}),
            ("yes",),
        ),
        Sample(
            "virtual-session",
            session,
            ("pool_acquire_timeout",),
            frozenset({"connection_timeout_ms", "query_delay_ms"}),
            ("yes",),
        ),
        Sample(
            "virtual-disk-full",
            disk,
            ("server_error",),
            frozenset(),
            ("no", "unsure"),
            flagged=True,
        ),
    ]


def heldout_samples(root: Path) -> list[Sample]:
    """프롬프트를 고칠 때 보지 않은 가상 재료 (2026-11-25 v2 승격 뒤 재확인) — 포화형·교착형·재현 불가형 하나씩.
    재현 불가형은 사유를 묻지 않는다 (빈 기대 = 아무 사유)."""
    base = incident_by_id(load_incidents(root), DEADLOCK)
    assert base is not None
    order = _virtual(
        base,
        "co:sw.backend:heldout-order-detail",
        symptom="점심 피크에 주문 상세 조회가 느려지고 일부 요청이 DB 연결 획득 시간 초과로 실패",
        root_cause="주문 상세를 만들 때 요청마다 재고·가격·쿠폰·배송 테이블을 차례로 조회해 요청 하나가 DB 문장 5개를 실행한다 — 연결을 쥔 채 다른 연결을 기다리지는 않지만 쥐는 시간이 길어 대기가 쌓인다",
        naive_fix="커넥션 풀 크기를 늘린다",
        why_naive_fails="피크 규모가 바뀌면 다시 대기가 쌓인다 — 요청당 DB 일을 줄이지 않았다",
        notes="풀 크기·피크 동시 요청 수는 확인되지 않음",
        naive_impl="주문 상세 API 가 요청마다 재고·가격·쿠폰·배송 정보를 각각 조회해 합친다",
        trigger="피크 시간 동시 요청 수가 커넥션 풀 크기보다 크다",
        why_naive_breaks="요청 하나가 DB 문장 5개를 실행해 연결을 오래 쥐고, 동시 요청이 풀보다 많으면 뒤 요청의 연결 대기가 대기 한도를 넘는다",
        hidden_requirement="피크 시간에도 주문 상세 조회가 DB 연결 대기로 실패하지 않는다",
        accepted_designs=[
            "[설계] 한 번의 조인 조회로 필요한 값을 함께 읽음 — 조건: 조인 결과 크기가 응답 한도 안",
            "[설계] 자주 바뀌지 않는 가격·쿠폰 정보를 메모리 캐시에서 읽음 — 조건: 캐시 만료가 가격 변경 반영 한도보다 짧음",
        ],
        reasons=["pool_acquire_timeout"],
        harness=["pool_size", "concurrency"],
    )
    notify = _virtual(
        base,
        "co:sw.backend:heldout-notify-record",
        symptom="알림 발송 요청이 몰리면 요청 전체가 멈췄다가 연결 획득 시간 초과로 실패",
        root_cause="발송 상태를 바꾸는 트랜잭션 안에서 발송 기록을 별도 트랜잭션으로 써, 요청 하나가 첫 연결을 쥔 채 두 번째 연결을 기다린다",
        naive_fix="연결 대기 한도를 늘린다",
        why_naive_fails="기다리는 시간만 길어지고 서로를 기다리는 상태는 그대로다",
        notes="풀 크기·동시 발송 수는 확인되지 않음",
        naive_impl="발송 상태 변경 트랜잭션 안에서 발송 기록 저장을 별도 트랜잭션으로 부른다",
        trigger="동시 발송 요청 수가 커넥션 풀 크기 이상이다",
        why_naive_breaks="요청마다 첫 연결을 쥔 채 두 번째 연결을 기다려, 동시 요청 수가 풀 크기 이상이면 모두가 서로를 기다린다",
        hidden_requirement="요청 하나가 같은 풀에서 연결을 둘 이상 동시에 쥐지 않는다",
        accepted_designs=[
            "[설계] 발송 기록을 같은 트랜잭션에서 씀 — 조건: 상태 변경이 롤백되면 기록도 함께 롤백돼도 됨",
            "[설계] 발송 기록을 트랜잭션이 끝난 뒤 씀 — 조건: 기록 누락을 다시 쓸 수 있음",
        ],
        reasons=["pool_acquire_timeout"],
        harness=["pool_size", "concurrency"],
    )
    payment = _virtual(
        base,
        "co:sw.backend:heldout-payment-gateway",
        symptom="외부 결제 승인 API 가 가끔 응답 없이 연결을 끊어 주문이 결제 대기 상태로 남는다",
        root_cause="결제 승인 응답을 받지 못하면 주문 상태를 바꾸지 않고 끝내, 실제로 승인된 결제가 주문에 반영되지 않는다",
        naive_fix="결제 승인 요청을 바로 다시 보낸다",
        why_naive_fails="이미 승인된 결제를 한 번 더 승인할 수 있다",
        notes="외부 API 의 끊김 빈도는 확인되지 않음",
        naive_impl="결제 승인 API 를 부르고 응답이 오면 주문 상태를 바꾼다",
        trigger="외부 결제 API 가 응답 없이 연결을 끊는다",
        why_naive_breaks="응답이 없으면 승인 여부를 모른 채 주문을 결제 대기로 둔다",
        hidden_requirement="응답이 끊겨도 결제 승인 여부를 확인해 주문 상태를 맞춘다",
        accepted_designs=[
            "[설계] 승인 요청에 멱등 키를 붙이고 끊기면 같은 키로 승인 여부를 조회 — 조건: 결제사가 키 조회를 지원",
            "[설계] 결제 대기 주문을 주기적으로 결제사와 대조 — 조건: 대조 주기가 고객 문의 한도보다 짧음",
        ],
        reasons=["server_error"],
        harness=[],
    )
    return [
        Sample(
            "heldout-order-saturation",
            order,
            ("pool_acquire_timeout",),
            frozenset({"connection_timeout_ms", "query_delay_ms"}),
            ("yes",),
        ),
        Sample(
            "heldout-notify-hold-and-wait",
            notify,
            ("pool_acquire_timeout",),
            frozenset({"pool_size", "concurrency"}),
            ("yes",),
        ),
        Sample(
            "heldout-payment-gateway",
            payment,
            (),
            frozenset(),
            ("no", "unsure"),
            flagged=True,
        ),
    ]


SETS = {"dev": samples, "heldout": heldout_samples}


def _pseudonymized(value: Any, pseudo: InterviewPseudonymizer, mapping: RunMapping) -> Any:
    """재료 블록의 글 칸을 가명화한다 — 운영의 인터뷰 재료는 저장 전에 가명화돼 있다 (공개 재료 notes 의 링크 같은 것이 토큰으로)."""
    if isinstance(value, str):
        return str(pseudo.pseudonymize(value, mapping))
    if isinstance(value, list):
        return [_pseudonymized(v, pseudo, mapping) for v in value]
    if isinstance(value, dict):
        return {k: _pseudonymized(v, pseudo, mapping) for k, v in value.items()}
    return value


def registry(tmp: Path, version: int) -> PromptRegistry:
    """평가할 판을 CURRENT 로 둔 임시 프롬프트 폴더."""
    dst = tmp / "prompts" / TASK
    dst.mkdir(parents=True)
    shutil.copy(REPO / "prompts" / TASK / f"v{version}.md", dst / f"v{version}.md")
    (dst / "CURRENT").write_text(f"v{version}\n")
    return PromptRegistry(tmp / "prompts")


@dataclass
class Row:
    sample: str
    run: int
    reason: str
    split_params: list[str]
    reproducible: str
    flags: list[str]
    reason_ok: bool
    params_ok: bool
    repro_ok: bool
    flag_ok: bool
    mechanism: str
    gaps: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


async def evaluate(
    version: int, runs: int, template_version: int, sample_set: str = "dev"
) -> list[Row]:
    settings = Settings()  # type: ignore[call-arg]
    llm = build_llm_client(settings)
    template = load_template(JAVA, template_version)
    rows: list[Row] = []
    with tempfile.TemporaryDirectory(prefix="material-analysis-eval-") as tmp:
        prompts = registry(Path(tmp), version)
        pseudo = InterviewPseudonymizer(
            Detectors(load_dictionaries(), use_kiwi=False), Path(tmp) / "mappings"
        )
        for run in range(runs):
            for s in SETS[sample_set](settings.incidents_dir):
                mapping = pseudo.new_mapping(f"int-{uuid.uuid4().hex}", "가상회사")
                a, prov = await analyze_material(
                    pseudo.stored_json(
                        _pseudonymized(material_block(s.incident), pseudo, mapping), mapping
                    ),
                    template,
                    llm=llm,
                    prompts=prompts,
                    ontology_version="onto-eval",
                )
                flags = analysis_flags(a, template, s.incident.feature)
                rows.append(
                    Row(
                        sample=s.name,
                        run=run,
                        reason=a.reason,
                        split_params=list(a.split_params),
                        reproducible=a.reproducible,
                        flags=flags,
                        reason_ok=not s.reasons or a.reason in s.reasons,
                        params_ok=s.params <= set(a.split_params),
                        repro_ok=a.reproducible in s.reproducible,
                        flag_ok=bool(flags) == s.flagged,
                        mechanism=a.mechanism,
                        gaps=list(a.gaps),
                        cost_usd=float(prov.cost_usd or 0.0),
                    )
                )
                r = rows[-1]
                print(
                    f"[{run}] {s.name}: 사유 {r.reason}{'' if r.reason_ok else ' ✗'} · 갈림 {r.split_params}"
                    f"{'' if r.params_ok else ' ✗'} · 재현 {r.reproducible}{'' if r.repro_ok else ' ✗'} · "
                    f"표시 {len(r.flags)}{'' if r.flag_ok else ' ✗'}"
                )
    return rows


def summary(rows: list[Row]) -> dict[str, Any]:
    n = len(rows) or 1
    return {
        "n": len(rows),
        "reason_ok": round(sum(r.reason_ok for r in rows) / n, 3),
        "params_ok": round(sum(r.params_ok for r in rows) / n, 3),
        "repro_ok": round(sum(r.repro_ok for r in rows) / n, 3),
        "flag_ok": round(sum(r.flag_ok for r in rows) / n, 3),
        "cost_usd": round(sum(r.cost_usd for r in rows), 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--version", type=int, default=1, help="평가할 analyze_material 판")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument(
        "--set",
        choices=sorted(SETS),
        default="dev",
        help="표본 묶음 — dev: 프롬프트를 고칠 때 본 것, heldout: 보지 않은 가상 재료",
    )
    ap.add_argument(
        "--template-version", type=int, default=4, help="보정 칸을 읽을 Java 템플릿 판 (기본 4)"
    )
    ap.add_argument(
        "--out", type=Path, help="결과 폴더 (git 밖, 예: data/eval/interview/material_analysis)"
    )
    a = ap.parse_args()
    rows = asyncio.run(evaluate(a.version, a.runs, a.template_version, a.set))
    s = summary(rows)
    print(json.dumps(s, ensure_ascii=False))
    if a.out is not None:
        out = a.out.resolve()
        if out.is_relative_to(REPO) and not out.is_relative_to(REPO / "data"):
            # 결과에는 LLM 이 쓴 기전 문장이 들어간다 — 나중에 표본에 회사 재료가 들어와도 git 에 남지 않게 (불변식 1)
            raise SystemExit(f"결과 폴더는 git 밖(레포의 data/ 아래)이어야 합니다: {a.out}")
        a.out.mkdir(parents=True, exist_ok=True)
        suffix = "" if a.set == "dev" else f"_{a.set}"
        path = a.out / f"{date.today().isoformat()}_v{a.version}_t{a.template_version}{suffix}.json"
        path.write_text(
            json.dumps(
                {"summary": s, "rows": [r.__dict__ for r in rows]}, ensure_ascii=False, indent=1
            ),
            "utf-8",
        )
        print(f"결과: {path}")


if __name__ == "__main__":
    main()
