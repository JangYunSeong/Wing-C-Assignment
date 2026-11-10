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


PER_REQUEST = "요청마다"
# 요청 하나를 가리키는 헤더 — 이름이나 제약으로 (요청 번호·멱등 키·추적 id)
_REQUEST_KEY = re.compile(
    r"request[-_]?id|idempot|correlation[-_]?id|trace[-_]?id|요청\s*번호|멱등", re.I
)


def per_request(contract: TaskContract, header: str) -> bool:
    """요청마다 새 값이어야 하는 헤더 (권한 칸 밖). 2026-11-10 retrydup-c: 요청 번호 X-Request-Id 를 도우미가 상수 하나로 줘
    모든 공개·숨은 테스트가 같은 번호를 공유 — 명세대로 번호로 중복을 막은 허용 구현 d2 가 서로 다른 충전을 재요청으로 보고
    다른 회원의 처음 결과를 돌려줘 떨어졌고, (회원·번호·금액)으로 느슨하게 막은 d1 만 통과했다."""
    decl = [h for e in contract.endpoints for h in e.headers if h.name == header]
    texts = [header, *(h.constraints or "" for h in decl)]
    return any(_REQUEST_KEY.search(x) for x in texts) or any(
        re.search(r"\buuid\b", h.constraints or "", re.I) for h in decl
    )


@dataclass(frozen=True)
class HeaderValues:
    name: str
    ok: str | None
    denied: str | None
    source: str  # "예시" · "아무 값" · "권한 규칙" — 도우미 주석에


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
    ruled = {
        e.auth.header: e.auth
        for e in contract.endpoints
        if e.auth is not None and e.auth.ok_value is not None
    }
    out: list[HeaderValues] = []
    for name in names:
        if (
            rule := ruled.get(name)
        ) is not None:  # 권한 칸에서 코드가 정한 요청자 행의 값 (auth_rule.fit_auth)
            out.append(HeaderValues(name, rule.ok_value, rule.denied_value, "권한 규칙"))
            continue
        if per_request(
            contract, name
        ):  # 요청마다 다른 값 — 상수 하나로 주면 모든 요청이 같은 요청이 된다
            out.append(HeaderValues(name, None, None, PER_REQUEST))
            continue
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


def _placeholders(contract: TaskContract, header: str) -> list[str]:
    """값 자리에 실제 값 대신 쓴 자리표시자·설명 (`X-Requester-Id=<MASTER 등급 멤버의 id>`)."""
    rx = re.compile(r"(?<![\w-])" + re.escape(header) + r"\s*[=:]\s*(<[^>]{1,60}>)", re.I)
    return [
        f"{t.test_id} 의 {m.group(1)}" for t in contract.public_tests for m in rx.finditer(t.checks)
    ]


def header_problems(contract: TaskContract) -> list[str]:
    """권한 거절(401·403)을 선언한 엔드포인트의 헤더인데 통과하는 값 예시가 없음 → 재요청 사유 (계약 검사 §3.3).
    값이 없으면 숨은·공개 테스트가 요청자를 지어내 요청이 거절에서 끝난다 (2026-11-08 알파: 계약 5개 중 2개가 '권한 없는 요청자'만 글로).

    2026-11-09 알파(cap2): 재요청 문구가 `<통과하는 값>` 꼴이라 답이 `<MASTER 등급 멤버의 memberId>` 로 돌아왔고, 그 권한을
    통과하는 행이 시드에 없었다 — 문구는 실제 값 예시와 시드 행을 함께 말한다."""
    out: list[str] = []
    ruled = {
        e.auth.header for e in contract.endpoints if e.auth is not None and e.auth.kind != "other"
    }
    other = {
        e.auth.header for e in contract.endpoints if e.auth is not None and e.auth.kind == "other"
    }
    for h in header_values(contract):
        if h.source == PER_REQUEST:
            continue
        if h.ok is not None or h.name in ruled or not _statuses(contract, h.name) & set(DENIED):
            continue  # 권한 칸(role·fixed)이 있으면 그쪽 검사가 말한다 (auth_rule.auth_problems)
        msg = (
            f"헤더 {h.name} 를 받는 엔드포인트가 권한 거절({'·'.join(map(str, DENIED))})을 내는데 통과하는 값의 예시가 없음 — "
            f"공개 테스트 checks 에 `{h.name}=1 → 200`, `{h.name}=7 → 403` 처럼 실제 값과 결과를 함께 적는다. "
            "권한이 요청자의 데이터(등급·소유 등)에 달렸으면 조건을 만족하는 행과 만족하지 않는 행을 시드 groups 에 두고 그 행의 값을 쓴다"
        )
        if h.name in other:
            # 2026-11-09 fixmt-be2: auth=other 인데 '공개 테스트에 요청자 값을 쓰지 않는다'는 지시(role·fixed 용)를 따라 값 없이
            # 3번 답해 계약이 거절됐다 — other 는 코드가 값을 정하지 않으니 이때만은 실제 값을 적어야 한다고 짚는다
            msg = (
                "auth 가 other 라 코드가 요청자 값을 정하지 않음 — 이때는 '통과하는 요청자로' 같은 말 대신 "
                + msg
            )
        if ph := _placeholders(contract, h.name):
            msg += f" (값 자리에 자리표시자를 씀: {'; '.join(ph[:3])} — 값으로 읽지 못함)"
        out.append(msg)
    return out
