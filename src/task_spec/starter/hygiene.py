"""비밀 값·외부 주소·원 사례 고유어 대조 — 생성기 산출물(정적 검사)과 계약(계약 검사)이 같은 규칙을 쓴다 (설계 검사 #332 T-F6).

코드가 계약에서 결정적으로 쓰는 파일(시드 SQL·조절값·계약 도우미·저장 확인 — hidden_helpers.code_written_paths)의 글은 계약에서 온다.
그 파일을 생성기 몫 정적 검사로 보면 생성기가 고칠 수 없는 재생성이 된다 (#319 공개 도우미 주석과 같은 꼴) — 그 글의 출처인 계약을
이 규칙으로 보고 계약 재요청으로 고친다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from task_spec.spec.feature import find_terms

SECRETS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|ghp|gho|xox[abp])[-_][A-Za-z0-9]{20,}"),
]
URL = re.compile(r"https?://([A-Za-z0-9.\-]+)")
LOCAL_HOSTS = re.compile(
    r"^(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|(?:[\w-]+\.)*example\.(?:com|org|net)|[\w-]+\.test|[\w-]+\.invalid)$"
)


@dataclass(frozen=True)
class HygieneHits:
    secret: bool = False
    hosts: list[str] = field(default_factory=list)
    named: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.secret or bool(self.hosts) or bool(self.named)


def hygiene_hits(text: str, avoid_terms: Sequence[str]) -> HygieneHits:
    """글 하나에서 비밀 값처럼 보이는 문자열·외부 네트워크 주소·원 사례 고유어."""
    return HygieneHits(
        secret=any(rx.search(text) for rx in SECRETS),
        hosts=sorted({h for h in URL.findall(text) if not LOCAL_HOSTS.match(h)}),
        named=find_terms(avoid_terms, text),
    )
