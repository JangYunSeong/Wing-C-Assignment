"""요청 헤더 값 — 계약 공개 테스트 예시에서 규칙을 통과하는 값·권한이 거절되는 값을 뽑는다 (도우미 상수의 근거).

2026-11-08 알파 (2df170e2): 계약은 권한 있는 요청자를 공개 테스트 예시(`X-Requester-Id=admin … → 200`)로만 말하는데,
숨은 테스트가 요청자를 `grader` 로 지어내 모든 요청이 403 에서 끝났다 — hc2(저장 실패 주입)는 저장 단계에 닿지 못해
기능 없는 시작 코드도 통과했다. 테스트가 값을 짐작하지 않게 도우미가 상수로 준다.

오탐을 줄이려고 좁게 읽는다: 계약 엔드포인트에 선언된 헤더 이름 바로 뒤의 값, 그 뒤에 처음 나오는 상태 코드 중
그 헤더를 받는 엔드포인트가 **선언한** 상태만 본다. 401·403 이면 거절되는 값, 그 밖이면 통과하는 값(권한은 통과했고
다른 규칙에서 갈림). 같은 값이 양쪽에 나오면 버린다.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from task_spec.schemas.build import TaskContract

DENIED = (401, 403)
# 예시가 없고 그 헤더를 받는 엔드포인트에 권한 거절 응답도 없으면 아무 값이나 받는 헤더 — 이 값을 준다
NEUTRAL = "grader"


@dataclass(frozen=True)
class HeaderValues:
    name: str
    ok: str | None
    denied: str | None
    source: str  # "예시" · "아무 값" — 도우미 주석에


def _statuses(contract: TaskContract, header: str) -> set[int]:
    return {
        r.status
        for e in contract.endpoints
        if any(h.name == header for h in e.headers)
        for r in e.responses
    }


def header_values(contract: TaskContract) -> list[HeaderValues]:
    """계약 헤더마다 (통과하는 값, 거절되는 값). 정할 수 없으면 None — 지어내지 않는다."""
    names = sorted({h.name for e in contract.endpoints for h in e.headers})
    out: list[HeaderValues] = []
    for name in names:
        declared = _statuses(contract, name)
        value_rx = re.compile(
            r"(?<![\w-])" + re.escape(name) + r"\s*[=:]\s*[\"'`]?([A-Za-z0-9_.@-]+)", re.I
        )
        ok: Counter[str] = Counter()
        bad: Counter[str] = Counter()
        for t in contract.public_tests:
            for m in value_rx.finditer(t.checks):
                status = next(
                    (
                        int(s.group(0))
                        for s in re.finditer(r"(?<!\d)[1-5]\d\d(?!\d)", t.checks[m.end() :])
                        if int(s.group(0)) in declared
                    ),
                    None,
                )
                if status is None:
                    continue
                (bad if status in DENIED else ok)[m.group(1)] += 1
        both = set(ok) & set(bad)
        ok_v = next((v for v, _ in ok.most_common() if v not in both), None)
        bad_v = next((v for v, _ in bad.most_common() if v not in both), None)
        if ok_v is None and bad_v is None and not declared & set(DENIED):
            out.append(HeaderValues(name, NEUTRAL, None, "아무 값"))
        else:
            out.append(HeaderValues(name, ok_v, bad_v, "예시"))
    return out


def header_problems(contract: TaskContract) -> list[str]:
    """권한 거절(401·403)을 선언한 엔드포인트의 헤더인데 통과하는 값 예시가 없음 → 재요청 사유 (계약 검사 §3.3).
    값이 없으면 숨은·공개 테스트가 요청자를 지어내 요청이 거절에서 끝난다 (2026-11-08 알파: 계약 5개 중 2개가 '권한 없는 요청자'만 글로)."""
    return [
        f"헤더 {h.name} 를 받는 엔드포인트가 권한 거절({'·'.join(map(str, DENIED))})을 내는데 통과하는 값의 예시가 없음 — "
        f"공개 테스트 checks 에 `{h.name}=<통과하는 값> → 200` 과 `{h.name}=<거절되는 값> → 403` 처럼 값과 결과를 함께 적는다"
        for h in header_values(contract)
        if h.ok is None and _statuses(contract, h.name) & set(DENIED)
    ]
