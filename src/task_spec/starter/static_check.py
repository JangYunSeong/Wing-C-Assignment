"""시작 코드 오버레이 정적 검사 `static_check` (docs/design/spec-to-code.md §6) — 결정적, LLM·빌드 없음.

생성기가 쓴 오버레이 파일(시작 코드·숨은 테스트·참조 구현)과 렌더한 README 를 빌드 전에 본다.
실패는 원인 생성기에 배정한다 (§7 수리): 재생성할 생성기(`gen_starter` 등)에 붙는 `problems` 와,
재생성으로 고칠 수 없어 승인자가 봐야 하는 `review_flags`(README = 승인된 명세 문장에서 나온 것·템플릿 기본값)를 나눈다.

검사
1. 경로 — kind 별 `fill_slots` 안, `locked` 밖, hidden 이 아니면 `reserved` 밖. 잠긴 하네스 자리를 고치면 따로 알린다. 파일 수·크기 상한.
2. 숨은 요구·운영 조건 낱말(재료 leak·trigger·원인 개념·먼저 말할 개념)·숨은 테스트 이름이 공개 파일(시작 코드·README·주는 공개 테스트)에
   — 코드 식별자는 낱말로 풀어서 본다 (connectionPool → connection pool). 미끼 코드(해법·문제 지점을 가리키는 주석·설정)도 이 낱말로 잡는다.
3. 운영 값이 시작 코드 설정에 박힘 — 템플릿 `harness_params[].config_keys` 의 값만 대조 (아무 숫자나 비교하면 8·32 같은 값이 오탐).
4. 비밀 값 · 외부 네트워크 주소 · 실존 회사·제품 이름(원 사례 고유어).
5. 숨은 테스트: 지원자 구현 내부를 import (게이트 ⑥ 사전 확인), 실패 사유 코드가 템플릿 목록 밖, 케이스 id 가 HiddenPlan 과 1:1 이 아님,
   조절값 파일을 손으로 씀(코드가 HiddenPlan 에서 쓴다 — `render.render_params`).
   공개 계약에 없는 API 를 부름 (manifest `hidden_rules.http_calls` 로 찾은 호출의 경로를 `{}` 자리로 정규화해 대조),
   한 파일에 같은 사유·문구의 단언이 둘 이상은 막지 않고 review_flags (`hidden_rules.reason_labels` — 사유가 잘못 붙었을 수 있음).
6. 테이블 이름 (manifest `naming.table`, starter/tables.py): 시작 코드가 선언한 테이블이 계약 테이블 밖이면 gen_starter, 숨은 테스트의 오류 주입 정규식이 계약 테이블 밖을 가리키거나 SQL 종류 없이 넓으면 gen_hidden.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from task_spec.schemas import FeatureSeed, GeneratedFile, HiddenPlan, TaskContract
from task_spec.schemas.build import FileKind
from task_spec.spec.feature import find_terms
from task_spec.starter.contract import SpecView, contract_fields, hidden_terms
from task_spec.starter.render import README, render_params
from task_spec.starter.tables import contract_tables, declared_tables, referenced_tables, same_table
from task_spec.starter.templates import Template, glob_match, slot_allows

KIND_SLOT = {
    "starter": "starter",
    "public_test": "public_test",
    "hidden": "hidden",
    "ref_naive": "ref",
    "ref_accepted": "ref",
}
KIND_TARGET = {
    "starter": "gen_starter",
    "public_test": "gen_starter",
    "hidden": "gen_hidden",
    "ref_naive": "gen_naive",
    "ref_accepted": "gen_accepted",
}

_SECRETS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|ghp|gho|xox[abp])[-_][A-Za-z0-9]{20,}"),
    re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\b\s*[:=]\s*['\"]?(?=[^\s'\"]*\d)(?=[^\s'\"]*[A-Za-z])[^\s'\"]{12,}"
    ),
]
_URL = re.compile(r"https?://([A-Za-z0-9.\-]+)")
_LOCAL_HOSTS = re.compile(
    r"^(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|(?:[\w-]+\.)*example\.(?:com|org|net)|[\w-]+\.test|[\w-]+\.invalid)$"
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CONFIG_SUFFIX = (".yml", ".yaml", ".properties")


@dataclass(frozen=True)
class Overlay:
    """생성기 산출물 한 벌 — 참조 구현은 변형마다 한 벌 (label 로 구분, 예: "naive-t1")."""

    kind: FileKind
    files: tuple[GeneratedFile, ...]
    label: str = ""

    @property
    def target(self) -> str:
        return KIND_TARGET[self.kind]

    @property
    def name(self) -> str:
        return f"{self.kind}{f'({self.label})' if self.label else ''}"


@dataclass(frozen=True)
class StaticProblem:
    target: str  # 재생성할 생성기 — gen_starter · gen_hidden · gen_naive · gen_accepted
    message: str


@dataclass
class StaticResult:
    problems: list[StaticProblem] = field(default_factory=list)
    review_flags: list[str] = field(default_factory=list)  # 재생성으로 못 고침 — 승인 화면에

    @property
    def ok(self) -> bool:
        return not self.problems

    def for_target(self, target: str) -> list[str]:
        return [p.message for p in self.problems if p.target == target]

    @property
    def targets(self) -> list[str]:
        return list(dict.fromkeys(p.target for p in self.problems))


def identifier_words(text: str) -> str:
    """코드 식별자를 낱말로 푼 글 — connectionPoolSize · max_pool → "connection pool size" · "max pool"."""
    words = []
    for ident in set(_IDENT.findall(text)):
        spaced = _CAMEL.sub(" ", ident).replace("_", " ").strip().lower()
        if " " in spaced:
            words.append(spaced)
    return "\n".join(sorted(words))


def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(node, dict):
        for k, v in node.items():
            out |= _flatten(v, f"{prefix}.{k}" if prefix else str(k))
    else:
        out[prefix] = node
    return out


def config_values(path: str, content: str) -> dict[str, str]:
    """설정 파일(yml·yaml·properties)의 점 표기 키 → 값 문자열. 읽지 못하면 빈 dict."""
    if path.endswith(".properties"):
        out: dict[str, str] = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "!")):
                continue
            kv = re.match(r"^([^=:\s]+)\s*[=:]\s*(.*)$", line)
            if kv:
                out[kv.group(1)] = kv.group(2).strip()
        return out
    try:
        docs = list(yaml.safe_load_all(content))
    except yaml.YAMLError:
        return {}
    flat: dict[str, str] = {}
    for doc in docs:
        flat |= {k: str(v) for k, v in _flatten(doc).items()}
    return flat


def _same_value(a: str, b: Any) -> bool:
    try:
        return float(str(a).replace(",", "").replace("_", "")) == float(b)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def _paths(o: Overlay, t: Template) -> list[StaticProblem]:
    m = t.manifest
    out: list[StaticProblem] = []
    locked_harness = (
        [
            f"{m.hidden_layout.harness}/*",
            f"{m.hidden_layout.selftest}/**",
        ]
        if m.hidden_layout
        else []
    )
    for f in o.files:
        if slot_allows(m, KIND_SLOT[o.kind], f.path):  # type: ignore[arg-type]
            continue
        if locked_harness and glob_match(f.path, locked_harness):
            msg = f"{o.name} 파일 {f.path} 는 잠긴 채점 하네스 자리 — 하네스는 고치지 않고 케이스 자리({m.hidden_layout.cases if m.hidden_layout else ''})에만 쓴다"
        else:
            msg = f"{o.name} 파일 {f.path} 는 쓸 수 없는 경로 (fill_slots 밖·잠긴 파일·채점 자리)"
        out.append(StaticProblem(o.target, msg))
    lim = m.limits
    if lim is not None:
        if len(o.files) > lim.max_files:
            out.append(
                StaticProblem(
                    o.target, f"{o.name} 파일 수 {len(o.files)} 가 상한 {lim.max_files} 를 넘음"
                )
            )
        big = [f.path for f in o.files if len(f.content.encode()) > lim.max_file_bytes]
        if big:
            out.append(
                StaticProblem(
                    o.target,
                    f"{o.name} 파일이 크기 상한 {lim.max_file_bytes}B 를 넘음: {', '.join(big)}",
                )
            )
        total = sum(len(f.content.encode()) for f in o.files)
        if total > lim.max_total_bytes:
            out.append(
                StaticProblem(
                    o.target, f"{o.name} 전체 크기 {total}B 가 상한 {lim.max_total_bytes}B 를 넘음"
                )
            )
    return out


def _hygiene(
    o: Overlay | None, files: Iterable[GeneratedFile], avoid_terms: Sequence[str]
) -> list[str]:
    """비밀 값·외부 주소·원 사례 고유어 — 메시지 목록 (배정은 호출하는 쪽)."""
    out: list[str] = []
    for f in files:
        where = f"{o.name} 파일 {f.path}" if o else f.path
        if any(rx.search(f.content) for rx in _SECRETS):
            out.append(f"{where} 에 비밀 값처럼 보이는 문자열이 있음")
        hosts = sorted({h for h in _URL.findall(f.content) if not _LOCAL_HOSTS.match(h)})
        if hosts:
            out.append(
                f"{where} 에 외부 네트워크 주소: {', '.join(hosts)} — 검증·채점은 네트워크 없이 돈다"
            )
        named = find_terms(avoid_terms, f.content)
        if named:
            out.append(f"{where} 에 원 사례의 실존 이름·고유어: {', '.join(named)}")
    return out


def _leaks(files: Iterable[GeneratedFile], terms: Sequence[str]) -> list[tuple[str, list[str]]]:
    hits = []
    for f in files:
        text = f"{f.path}\n{f.content}\n{identifier_words(f.content)}\n{identifier_words(f.path)}"
        shown = find_terms(terms, text)
        if shown:
            hits.append((f.path, shown))
    return hits


def _operating_values(
    o: Overlay, t: Template, plan: HiddenPlan
) -> tuple[list[StaticProblem], list[str]]:
    problems: list[StaticProblem] = []
    flags: list[str] = []
    keyed = [
        (p, k) for p in t.manifest.harness_params if p.name in plan.harness for k in p.config_keys
    ]
    if not keyed:
        return problems, flags
    base = {
        k: v
        for rel in t.files("project")
        if rel.endswith(_CONFIG_SUFFIX)
        for k, v in config_values(
            rel, (t.project / rel).read_text("utf-8", errors="ignore")
        ).items()
    }
    for f in o.files:
        if not f.path.endswith(_CONFIG_SUFFIX):
            continue
        cfg = config_values(f.path, f.content)
        for p, key in keyed:
            if key in cfg and _same_value(cfg[key], plan.harness[p.name]):
                msg = (
                    f"{o.name} 설정 {f.path} 의 {key}={cfg[key]} 가 채점 운영 값({p.name})과 같음 — "
                    "운영 값은 채점 때 하네스가 넣는다, 시작 코드에는 개발용 값만"
                )
                if key in base and _same_value(base[key], plan.harness[p.name]):
                    flags.append(
                        f"템플릿 기본 설정 {key} 가 운영 값과 같음 — 템플릿 기본값을 바꿀 신호"
                    )
                else:
                    problems.append(StaticProblem(o.target, msg))
    return problems, flags


def _call_arg(text: str, start: int) -> str:
    """start 부터 괄호 깊이 0 의 `,` 또는 `)` 앞까지 — 호출 인자 식 하나."""
    depth, i, quote = 0, start, False
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = False
        elif ch == '"':
            quote = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        i += 1
    return text[start:i]


def _split_plus(expr: str) -> list[str]:
    parts, depth, quote, cur = [], 0, False, ""
    for i, ch in enumerate(expr):
        if quote:
            quote = not (ch == '"' and expr[i - 1] != "\\")
        elif ch == '"':
            quote = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "+" and depth == 0:
            parts.append(cur.strip())
            cur = ""
            continue
        cur += ch
    return [*parts, cur.strip()]


_LITERAL = re.compile(r'^"((?:[^"\\]|\\.)*)"$')


def path_template(expr: str) -> str | None:
    """`"/orders/" + id + "/status"` → `/orders/{}/status`. 첫 조각이 "/…" 문자열이 아니면 None (판단 안 함). 쿼리는 뺀다."""
    out: list[str] = []
    for n, part in enumerate(_split_plus(expr)):
        lit = _LITERAL.match(part)
        if n == 0 and not (lit and lit.group(1).startswith("/")):
            return None
        out.append(lit.group(1) if lit else "{}")
    return "".join(out).split("?", 1)[0]


def http_calls(content: str, patterns: Sequence[str]) -> list[tuple[str, str]]:
    """(메서드, 경로 틀) — 경로를 판단할 수 없는 호출은 뺀다."""
    found: list[tuple[str, str]] = []
    for pat in patterns:
        for m in re.finditer(pat, content):
            path = path_template(_call_arg(content, m.end()))
            if path is not None:
                method = (m.groupdict().get("method") or "GET").upper()
                found.append((method, path))
    return found


def _contract_api(contract: TaskContract) -> list[tuple[str, re.Pattern[str]]]:
    return [
        (
            e.method.upper(),
            re.compile("^" + re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(e.path)) + "/?$"),
        )
        for e in contract.endpoints
        if e.method
    ]


def _unknown_calls(
    files: Iterable[GeneratedFile], contract: TaskContract, patterns: Sequence[str]
) -> list[str]:
    api = _contract_api(contract)
    out: list[str] = []
    for f in files:
        bad = sorted(
            {
                f"{method} {path}"
                for method, path in http_calls(f.content, patterns)
                if not any(method == m and rx.match(path) for m, rx in api)
            }
        )
        if bad:
            out.append(
                f"숨은 테스트 {f.path} 가 공개 계약에 없는 API 를 부름: {', '.join(bad)} "
                f"— 계약 API({', '.join(e.method + ' ' + e.path for e in contract.endpoints if e.method)})만 쓴다"
            )
    return out


def _starter_tables(o: Overlay, t: Template, contract: TaskContract | None) -> list[StaticProblem]:
    """시작 코드가 선언한 테이블(CREATE TABLE·@Table 등)은 계약이 정한 이름이어야 한다 — 숨은 테스트가 그 이름으로 오류를 주입한다."""
    naming = t.manifest.naming.table
    if contract is None or naming is None or not naming.declare_patterns:
        return []
    expected = contract_tables(contract, naming)
    if not expected:
        return []
    names = {m.entity_id: m.name for m in contract.entities}
    stray = [
        d
        for d in declared_tables(o.files, naming.declare_patterns)
        if not any(same_table(d, e, naming) for e in expected.values())
    ]
    if not stray:
        return []
    want = ", ".join(f"{names[i]} → {n}" for i, n in expected.items())
    return [
        StaticProblem(
            "gen_starter",
            f"시작 코드의 테이블 {', '.join(stray)} 이 계약 테이블 밖 — 계약이 정한 이름만 쓴다 ({want})",
        )
    ]


_RELATION_ANNOTATIONS = {"manytoone", "onetoone"}


def _starter_foreign_keys(
    o: Overlay, t: Template, contract: TaskContract | None
) -> list[StaticProblem]:
    """시작 코드는 계약에 없는 외래 키를 만들지 않는다 (계약 엔티티 foreign_keys 만).
    2026-11-07 루프: 계약에 관계가 없는데 시작 코드가 감사 기록 → 배송 건 외래 키를 걸어, 계약이 말하는 '없는 배송 건 요청도 기록'이 DB 에서 거부됐다."""
    naming = t.manifest.naming.table
    if contract is None or naming is None or not naming.fk_patterns:
        return []
    allowed = {fk_field.lower() for m in contract.entities for fk_field, _ in m.fk_pairs()}
    bad: list[str] = []
    for f in o.files:
        for pat in naming.fk_patterns:
            for mt in re.finditer(pat, f.content, re.I):
                name = mt.group(1)
                ok = (
                    bool(allowed)
                    if name.lower() in _RELATION_ANNOTATIONS
                    else name.replace("_", "").lower() in allowed
                )
                if not ok:
                    bad.append(f"{f.path}: {name}")
    if not bad:
        return []
    declared = (
        ", ".join(f"{m.name}.{a} → {b}" for m in contract.entities for a, b in m.fk_pairs())
        or "없음"
    )
    return [
        StaticProblem(
            "gen_starter",
            f"시작 코드가 계약에 없는 외래 키를 둠: {'; '.join(sorted(set(bad)))} — 계약의 외래 키({declared})만 둔다 "
            "(없는 대상의 요청도 기록하는 테이블에 외래 키를 걸면 그 기록이 거부된다)",
        )
    ]


def _hidden_tables(
    files: Iterable[GeneratedFile], t: Template, contract: TaskContract
) -> list[str]:
    naming, rules = t.manifest.naming.table, t.manifest.hidden_rules
    if naming is None or not rules.table_refs:
        return []
    expected = list(contract_tables(contract, naming).values())
    out: list[str] = []
    for f in files:
        bad = [
            r
            for r in referenced_tables(f.content, rules.table_refs, rules.string_literal)
            if not any(same_table(r, e, naming) for e in expected)
        ]
        if bad:
            out.append(
                f"숨은 테스트 {f.path} 의 오류 주입 정규식이 계약에 없는 테이블을 가리킴: {', '.join(bad)} "
                f"— 계약의 테이블 이름({', '.join(expected) or '없음'})을 그대로 쓴다 (경로에서 짐작하지 않는다)"
            )
        broad = _broad_injections(f.content, rules.table_refs, rules.string_literal, expected)
        if broad:
            out.append(
                f"숨은 테스트 {f.path} 의 오류 주입 정규식이 SQL 종류 없이 넓음: {'; '.join(broad)} — "
                "`^\\s*update\\s+<테이블>\\b` 처럼 SQL 종류 + 계약 테이블로 시작하고, 숨은 요구가 말하는 그 단계만 주입한다 "
                "(테이블 이름만 쓰면 실패 기록 쓰기까지 막혀 어떤 설계도 통과 못 한다)"
            )
    return out


_INTEGER_TYPE = re.compile(r"^\s*(?:long|int|integer|bigint|short|number)\b", re.I)
# 그대로 두는 단언: null·형식 확인, 기대값을 Integer 로 맞춘 비교 (2026-11-07 배송 감사 숨은 테스트 `isEqualTo(((Number) id).intValue())` — 작은 id 라 동작)
_NULL_CHECK = re.compile(r"\.is(?:Not)?Null\(\)|\.isInstanceOf\(|\.intValue\(\)")


def _raw_number_asserts(
    files: Iterable[GeneratedFile], patterns: Sequence[str], contract: TaskContract
) -> list[str]:
    """계약에서 정수 형식인 필드를 JSON Map 값 그대로 단언 — 크기에 따라 Integer·Long 이라 long 과 비교하면 모든 구현이 실패.
    null 확인(isNull·isNotNull)·형식 확인(isInstanceOf)·기대값을 Integer 로 맞춘 비교(intValue)는 그대로 둔다."""
    numeric = {f.name for _, f in contract_fields(contract) if _INTEGER_TYPE.match(f.type)}
    if not numeric:
        return []
    out: list[str] = []
    for f in files:
        hits: list[str] = []
        for pat in patterns:
            for m in re.finditer(pat, f.content):
                end = f.content.find(";", m.end())
                tail = f.content[m.end() : end if end >= 0 else None]
                if m.group(1) in numeric and not _NULL_CHECK.search(tail):
                    hits.append(m.group(0))
        if hits:
            shown = "; ".join(dict.fromkeys(hits))
            field = re.search(r'"(\w+)"', hits[0])
            name = field.group(1) if field else "id"
            out.append(
                f"숨은 테스트 {f.path} 가 숫자 필드를 JSON Map 값 그대로 단언함: {shown} — JSON 숫자는 크기에 따라 "
                f'Integer·Long 으로 읽혀 long 과 비교하면 모든 구현이 실패한다. ((Number) x.get("{name}")).longValue() 로 바꿔 비교한다'
            )
    return out


_SQL_WORDS = re.compile(r"\b(?:update|insert|delete|select|into|from)\b", re.I)


def _broad_injections(
    content: str, table_refs: Sequence[str], literal: str, tables: Sequence[str]
) -> list[str]:
    """SQL 을 가리키는 정규식(계약 테이블 이름이나 SQL 단어가 든 것)인데 'SQL 종류 + 테이블' 꼴로 시작하지 않는 것.
    2026-11-07 루프: 다시 만든 숨은 테스트가 `failCommitAfter = "delivery|delivery_audit_log"` 로 실패 기록 INSERT 의 커밋까지 막았다."""
    out: list[str] = []
    for lit in re.finditer(literal, content):
        text = lit.group(1)
        sqlish = _SQL_WORDS.search(text) or any(t.lower() in text.lower() for t in tables)
        if (
            sqlish
            and not any(re.search(pat, text, re.I) for pat in table_refs)
            and f'"{text}"' not in out
        ):
            out.append(f'"{text}"')
    return out


def _hidden_cases(
    o: Overlay, t: Template, plan: HiddenPlan, contract: TaskContract | None = None
) -> list[StaticProblem]:
    m = t.manifest
    rules = m.hidden_rules
    out: list[str] = []
    imports = [re.compile(p, re.M) for p in rules.forbidden_imports]
    reason_rx = [re.compile(p) for p in rules.reason_patterns]
    case_rx = re.compile(rules.case_id_pattern)
    params_path = m.hidden_layout.params if m.hidden_layout else None
    seen_ids: Counter[str] = Counter()
    used_codes: set[str] = set()
    for f in o.files:
        if f.path == params_path:
            if f.content != render_params(plan, m).content:
                out.append(
                    f"조절값 파일 {f.path} 를 손으로 씀 — 이 파일은 숨은 테스트에 넣지 않는다 (HiddenPlan 에서 코드가 쓴다)"
                )
            continue
        lines = f.content.splitlines()
        bad = [line.strip() for line in lines if any(rx.search(line) for rx in imports)]
        if bad:
            out.append(
                f"숨은 테스트 {f.path} 가 지원자 구현 내부를 직접 씀: {'; '.join(bad[:3])} — 공개 계약(HTTP)으로만 판정한다"
            )
        for rx in reason_rx:
            used_codes |= {mt.group(1) for mt in rx.finditer(f.content)}
        seen_ids.update(set(case_rx.findall(f.content)))
    if contract is not None and rules.http_calls:
        out += _unknown_calls(
            [f for f in o.files if f.path != params_path], contract, rules.http_calls
        )
    if contract is not None:
        out += _hidden_tables([f for f in o.files if f.path != params_path], t, contract)
    if contract is not None and rules.raw_number_asserts:
        out += _raw_number_asserts(
            [f for f in o.files if f.path != params_path], rules.raw_number_asserts, contract
        )
    bad_codes = sorted(used_codes - m.reason_codes)
    if bad_codes:
        out.append(f"숨은 테스트의 실패 사유 코드가 템플릿 목록 밖: {', '.join(bad_codes)}")
    planned = {c.case_id for c in plan.cases}
    missing = sorted(planned - set(seen_ids))
    extra = sorted(set(seen_ids) - planned)
    twice = sorted(i for i, n in seen_ids.items() if n > 1 and i in planned)
    if missing:
        out.append(f"HiddenPlan 케이스가 숨은 테스트에 없음: {', '.join(missing)}")
    if extra:
        out.append(f"HiddenPlan 에 없는 케이스 id 가 숨은 테스트에 있음: {', '.join(extra)}")
    if twice:
        out.append(
            f"케이스 id 가 여러 파일에 나옴 (케이스 하나 = 테스트 파일 하나): {', '.join(twice)}"
        )
    return [StaticProblem("gen_hidden", msg) for msg in out]


def _duplicate_labels(o: Overlay, t: Template) -> list[str]:
    """같은 사유·문구의 단언이 한 케이스 파일에 둘 이상 — 사유가 잘못 붙었을 수 있어 승인자에게 표시 (막지 않음).
    서로 다른 요청을 같은 문구로 확인하는 것은 정상일 수 있다 (2026-11-07: 재요청 문제로 두었더니 생성 자체가 실패)."""
    rules = t.manifest.hidden_rules
    flags: list[str] = []
    for f in o.files:
        labels: Counter[tuple[str, str]] = Counter()
        for pat in rules.reason_labels:
            labels.update((m.group(1), m.group(2)) for m in re.finditer(pat, f.content))
        dup = sorted(f'{code} "{msg}"' for (code, msg), n in labels.items() if n > 1)
        if dup:
            flags.append(
                f"숨은 테스트 {f.path} 에 같은 사유·문구의 단언이 둘 이상: {'; '.join(dup)} — 사유가 단언과 맞는지 확인 "
                "(흔한 구현이 '의도한 사유'로 실패한 것처럼 보일 수 있음)"
            )
    return flags


def static_check(
    overlays: Sequence[Overlay],
    *,
    template: Template,
    plan: HiddenPlan,
    spec: SpecView,
    contract: TaskContract | None = None,
    seed: FeatureSeed | None = None,
    readme: GeneratedFile | None = None,
    avoid_terms: Sequence[str] = (),
    public_tests_visible: bool = False,
) -> StaticResult:
    """오버레이 정적 검사 (§6). problems 는 생성기별 재생성 사유, review_flags 는 승인자에게."""
    res = StaticResult()
    hidden_files = [f for o in overlays if o.kind == "hidden" for f in o.files]
    hidden_names = [
        PurePosixPath(f.path).stem
        for f in hidden_files
        if not (
            template.manifest.hidden_layout and f.path == template.manifest.hidden_layout.params
        )
    ]
    terms = list(
        dict.fromkeys([*hidden_terms(seed, spec), *(n for n in hidden_names if len(n) >= 4)])
    )

    for o in overlays:
        res.problems += _paths(o, template)
        res.problems += [StaticProblem(o.target, msg) for msg in _hygiene(o, o.files, avoid_terms)]
        public = o.kind == "starter" or (o.kind == "public_test" and public_tests_visible)
        if public:
            for path, shown in _leaks(o.files, terms):
                res.problems.append(
                    StaticProblem(
                        o.target,
                        f"공개 파일 {path} 에 숨은 요구·운영 조건·숨은 테스트를 드러내는 말: {', '.join(shown)} "
                        "— 코드·주석·이름에 쓰지 않는다 (미끼 코드 포함)",
                    )
                )
        if o.kind == "starter":
            problems, flags = _operating_values(o, template, plan)
            res.problems += problems
            res.problems += _starter_tables(o, template, contract)
            res.problems += _starter_foreign_keys(o, template, contract)
            res.review_flags += flags
        if o.kind == "hidden":
            res.problems += _hidden_cases(o, template, plan, contract)
            res.review_flags += _duplicate_labels(o, template)

    if readme is not None:
        # README 는 승인된 명세 문장으로 결정적으로 만든다 — 걸리면 명세 문제 (재생성으로 못 고침)
        for _, shown in _leaks([readme], hidden_terms(seed, spec)):
            res.review_flags.append(
                f"{README} 에 숨은 요구·운영 조건을 드러내는 말: {', '.join(shown)} — 승인된 명세 문장에서 나옴, 명세 수정 필요"
            )
        res.review_flags += _hygiene(None, [readme], avoid_terms)
    res.review_flags = list(dict.fromkeys(res.review_flags))
    return res


def overlay_from_dir(kind: FileKind, root: Path, label: str = "") -> Overlay:
    """폴더 안 파일 → Overlay (평가·테스트용). 경로는 root 기준."""
    files = tuple(
        GeneratedFile(path=p.relative_to(root).as_posix(), content=p.read_text("utf-8"))
        for p in sorted(root.rglob("*"))
        if p.is_file()
    )
    return Overlay(kind=kind, files=files, label=label)
