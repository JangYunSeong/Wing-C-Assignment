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

import ast
import difflib
import functools
import posixpath
import re
from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from task_spec.schemas import FeatureSeed, GeneratedFile, HiddenPlan, TaskContract
from task_spec.schemas.build import Endpoint, FileKind
from task_spec.schemas.template import TemplateManifest
from task_spec.spec.feature import find_terms
from task_spec.starter.contract import SpecView, contract_fields, existing_endpoints, hidden_terms
from task_spec.starter.header_values import header_values
from task_spec.starter.hidden_helpers import (
    key_fields,
    render_contract_case,
    render_persistence_check,
)
from task_spec.starter.render import README, render_params
from task_spec.starter.seed_sql import schema_problems
from task_spec.starter.tables import (
    contract_tables,
    declared_tables,
    referenced_tables,
    same_table,
    spring_snake,
)
from task_spec.starter.templates import Template, glob_match, is_java, slot_allows

KIND_SLOT = {
    "starter": "starter",
    "public_test": "public_test",
    "hidden": "hidden",
    "ref_naive": "ref",
    "ref_accepted": "ref",
}
KIND_TARGET = {
    "starter": "gen_starter",
    "public_test": "gen_public",
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


HARNESS_OVERRIDE = "채점 때 하네스가 덮어쓰는 설정"


def _config_map(files: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {
        k: v
        for path, content in files
        if path.endswith(_CONFIG_SUFFIX)
        for k, v in config_values(path, content).items()
    }


def _harness_overrides(
    o: Overlay, starter: Overlay | None, t: Template, plan: HiddenPlan
) -> list[StaticProblem]:
    """허용 구현이 하네스 조절값의 설정 키를 바꿈 — 채점 때 운영 값으로 덮어써지므로 그 설정에 기댄 설계는 채점에서 무효
    (알파 테스트 2026-11-07: 허용 설계 '풀 크기 산정'을 하네스가 pool_size 로 덮어써 어떤 구현도 통과할 수 없었다)."""
    keys = {k for p in t.manifest.harness_params if p.name in plan.harness for k in p.config_keys}
    if not keys:
        return []
    base = _config_map(
        (rel, (t.project / rel).read_text("utf-8", errors="ignore"))
        for rel in t.files("project")
        if rel.endswith(_CONFIG_SUFFIX)
    )
    if starter is not None:
        base |= _config_map((f.path, f.content) for f in starter.files)
    out: list[StaticProblem] = []
    for f in o.files:
        cfg = _config_map([(f.path, f.content)])
        for key in sorted(keys & cfg.keys()):
            if key in base and str(base[key]).strip() == str(cfg[key]).strip():
                continue  # 시작 코드 값 그대로 — 바꾸지 않음
            out.append(
                StaticProblem(
                    o.target,
                    f"{o.name} 설정 {f.path} 의 {key}={cfg[key]} 는 {HARNESS_OVERRIDE}다 — "
                    "이 값을 바꿔 숨은 요구를 피하는 설계는 채점에서 효과가 없다. 운영 값은 코드로도 바꾸지 않는다 — "
                    "숨은 요구는 연결을 쓰는 방식(트랜잭션 경계·기록 시점)으로 푼다",
                )
            )
    return out


# 코드로 연결 풀·데이터소스·요청 스레드를 만들거나 바꾸는 것 — 하네스가 넣는 운영 값(@DynamicPropertySource)을 우회한다.
# 2026-11-09 v19-loop1·fixmt-be: 허용 설계 '풀 크기 산정'(d3)을 설정으로 못 하자 구현이 DataSource 빈을 새로 만들거나
# BeanPostProcessor 로 만들어진 풀을 80 으로 바꿔 통과했다 (채점 조건 우회 — 지원자도 같은 길로 함정을 피할 수 있다).
_CODE_OVERRIDES = (
    ("DataSourceBuilder", re.compile(r"\bDataSourceBuilder\b")),
    ("new HikariDataSource/HikariConfig", re.compile(r"\bnew\s+Hikari(?:DataSource|Config)\s*\(")),
    (
        "풀 크기·대기 시간 변경",
        re.compile(r"\.set(?:MaximumPoolSize|MinimumIdle|ConnectionTimeout)\s*\("),
    ),
    ("DataSource 빈", re.compile(r"@Bean\b[^{;]*?\bDataSource\s+\w+\s*\(", re.S)),
    ("요청 스레드 수 변경", re.compile(r"\.set(?:MaxThreads|MinSpareThreads)\s*\(")),
)


def _code_overrides(o: Overlay, t: Template) -> list[StaticProblem]:
    """구현이 코드로 운영 값(연결 풀·데이터소스·요청 스레드)을 만들거나 바꿈 — 설정 키를 바꾸는 것(_harness_overrides)과 같은 우회."""
    if not is_java(t.manifest):
        return []
    out: list[StaticProblem] = []
    for f in o.files:
        if not f.path.endswith(".java"):
            continue
        body = _strip_comments(f.content)
        hits = [name for name, rx in _CODE_OVERRIDES if rx.search(body)]
        if hits:
            out.append(
                StaticProblem(
                    o.target,
                    f"{o.name} {f.path} 가 코드로 운영 값을 만들거나 바꿈 ({', '.join(hits)}) — {HARNESS_OVERRIDE}(연결 풀·대기 시간·"
                    "요청 스레드)를 코드로 우회하는 것이다. 운영 값은 채점 환경이 정한다 — 숨은 요구는 연결을 쓰는 방식으로 푼다",
                )
            )
    return out


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
    return _endpoint_rx(contract.endpoints)


def _endpoint_rx(endpoints: Iterable[Endpoint]) -> list[tuple[str, re.Pattern[str]]]:
    return [
        (
            e.method.upper(),
            re.compile("^" + re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(e.path)) + "/?$"),
        )
        for e in endpoints
        if e.method
    ]


def _unavailable_imports(o: Overlay, t: Template) -> list[StaticProblem]:
    """템플릿 클래스패스에 없는 패키지 import — 컴파일이 반드시 실패하므로 Gradle 전에 그 생성기에."""
    rx = [re.compile(p, re.M) for p in t.manifest.unavailable_imports]
    out: list[StaticProblem] = []
    for f in o.files:
        bad = sorted({m.group(0).strip() for r in rx for m in r.finditer(f.content)})
        if bad:
            out.append(
                StaticProblem(
                    o.target,
                    f"{f.path} 가 템플릿에 없는 라이브러리를 씀: {'; '.join(bad)} — 템플릿 의존성만 쓴다 (입력 검증은 직접 확인하고 계약의 오류 응답으로)",
                )
            )
    return out


_REL_IMPORT = re.compile(
    r"""^\s*(?:import|export)\b[^;]*?\bfrom\s+['"](\.{1,2}/[^'"]+)['"]""", re.M
)
_TS_EXTS = ("", ".ts", ".tsx", "/index.ts", "/index.tsx")


def _test_folder_imports(o: Overlay, t: Template) -> list[StaticProblem]:
    """구현(시작 코드·참조 구현)이 채점 자리(reserved)나 공개 테스트 자리의 파일을 import — 그 파일은 대상마다 있거나 없어서
    (시작 코드 + 숨은 테스트 실행에는 공개 테스트가 없다) 컴파일이 깨지고, 지원자 이미지에도 없다. TypeScript 상대 import 만 (Java 는 패키지).
    2026-11-09 React 알파: 시작 코드·참조 구현 셋이 공개 도우미(src/__public__/contract.ts)의 응답 타입을 import 했다."""
    m = t.manifest
    if is_java(m):
        return []
    off_limits = [*m.reserved, *m.fill_slots.get("public_test", [])]
    out: list[StaticProblem] = []
    for f in o.files:
        bad = []
        for spec in _REL_IMPORT.findall(f.content):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(f.path), spec))
            if any(glob_match(target + ext, off_limits) for ext in _TS_EXTS):
                bad.append(spec)
        if bad:
            out.append(
                StaticProblem(
                    o.target,
                    f"{f.path} 가 채점·공개 테스트 폴더의 파일을 import 함: {', '.join(sorted(set(bad)))} — 구현은 테스트 도우미를 쓰지 않는다. "
                    "필요한 타입은 계약의 데이터 모양대로 src/ 안에 직접 정의한다",
                )
            )
    return out


def _starter_schema(o: Overlay, t: Template, contract: TaskContract | None) -> list[StaticProblem]:
    """시작 코드 스키마가 코드가 쓸 시드와 맞는가 (starter/seed_sql.schema_problems) — 맞지 않으면 기동이 깨진다."""
    rule = t.manifest.seed_sql
    if contract is None or rule is None:
        return []
    schema = next((f.content for f in o.files if f.path == rule.schema_path), None)
    return [StaticProblem("gen_starter", p) for p in schema_problems(schema, contract, t.manifest)]


def _starter_new_api_tests(
    o: Overlay, t: Template, contract: TaskContract | None, spec: SpecView
) -> list[StaticProblem]:
    """시작 코드의 기존 기능 테스트가 새로 만드는 API 를 부르면 새 기능을 구현한 것 — 새 API 는 지원자 몫이라 시작 코드에서 자리 표시다.
    이미 있는 API 가 하나도 없으면 판단하지 않는다 (무엇이 기존인지 모름)."""
    patterns = t.manifest.starter_test_calls
    if contract is None or not patterns:
        return []
    existing = existing_endpoints(contract, spec)
    new = [e for e in contract.endpoints if e.method and e not in existing]
    if not existing or not new:
        return []
    new_rx = _endpoint_rx(new)
    out: list[StaticProblem] = []
    for f in o.files:
        if "/test/" not in f"/{f.path}":
            continue
        calls = sorted(
            {
                f"{method} {path}"
                for method, path in http_calls(f.content, patterns)
                if any(method == m and rx.match(path) for m, rx in new_rx)
            }
        )
        if calls:
            out.append(
                StaticProblem(
                    "gen_starter",
                    f"시작 코드의 기존 기능 테스트 {f.path} 가 새로 만드는 API 를 부름: {', '.join(calls)} — 새 API 는 지원자 몫이라 "
                    "시작 코드에서는 자리 표시(TODO)다. 기존 기능 테스트는 이미 있는 API("
                    + ", ".join(f"{e.method} {e.path}" for e in existing)
                    + ")의 공개 요구 전 동작만 확인한다",
                )
            )
    return out


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


_CONST_JOIN = re.compile(r'"\s*\+\s*(TABLE_[A-Z0-9_]+)\s*\+\s*"')
_CONST_TAIL = re.compile(r'"\s*\+\s*(TABLE_[A-Z0-9_]+)\b')
_CONST_HEAD = re.compile(r'\b(TABLE_[A-Z0-9_]+)\s*\+\s*"')


def inline_table_constants(content: str, consts: dict[str, str]) -> str:
    """`"^\\s*update\\s+" + TABLE_ORDER + "\\b"` → `"^\\s*update\\s+orders\\b"` — 계약 도우미의 테이블 상수를 문자열에 끼워 넣어
    문자열 상수만 보는 검사(오류 주입 정규식의 테이블·넓은 주입)가 이어 붙인 정규식도 읽게 한다.
    2026-11-08: ContractCase 가 TABLE_ 상수를 주자 숨은 테스트가 그것을 이어 붙였고, 첫 조각만 본 검사가 '넓은 주입'으로 오탐해 생성 실패."""
    if not consts:
        return content

    def val(m: re.Match[str]) -> str:
        return consts.get(m.group(1), m.group(1))

    content = _CONST_JOIN.sub(val, content)
    content = _CONST_TAIL.sub(lambda m: val(m) + '"', content)
    return _CONST_HEAD.sub(lambda m: '"' + val(m), content)


def _hidden_tables(
    files: Iterable[GeneratedFile], t: Template, contract: TaskContract
) -> list[str]:
    naming, rules = t.manifest.naming.table, t.manifest.hidden_rules
    if naming is None or not rules.table_refs:
        return []
    tables = contract_tables(contract, naming)
    names = {m.entity_id: m.name for m in contract.entities}
    consts = {f"TABLE_{spring_snake(names[e]).upper()}": v for e, v in tables.items()}
    expected = list(tables.values())
    files = [
        GeneratedFile(path=f.path, content=inline_table_constants(f.content, consts)) for f in files
    ]
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


def _reason_rules(
    cases: Iterable[GeneratedFile],
    m: TemplateManifest,
    plan: HiddenPlan,
    case_rx: re.Pattern[str],
    reason_rx: list[re.Pattern[str]],
) -> list[str]:
    """의도한 사유가 제대로 나오게 — (1) 사유를 붙이면 안 되는 단언(모든 실패를 세는 것)에 붙임, (2) 사유가 요구하는 것(오류 주입·운영 피크)을
    케이스가 쓰지 않음. 2026-11-08 숨은 테스트 검토: 사유를 Burst.wrong 에 붙임 14/14, 저장 실패 사유인데 주입 없음 4/5."""
    intended = {c.case_id: c.intended_reason for c in plan.cases}
    requires = {r.code: r.requires for r in m.failure_reasons if r.requires}
    bad_on = [re.compile(p) for p in m.hidden_rules.reason_not_on]
    out: list[str] = []
    for f in cases:
        if bad_on:
            hits = [
                " ".join(stmt.split())[:120]
                for stmt in f.content.split(";")
                if any(rx.search(stmt) for rx in reason_rx)
                and any(rx.search(stmt) for rx in bad_on)
            ]
            if hits and m.hidden_rules.reason_not_on_note:
                out.append(
                    f"숨은 테스트 {f.path} 가 사유를 붙이면 안 되는 단언에 사유를 붙임: {hits[0]} — "
                    + m.hidden_rules.reason_not_on_note
                )
            elif hits:
                out.append(
                    f"숨은 테스트 {f.path} 가 모든 실패를 세는 단언에 사유를 붙임: {hits[0]} — 어떤 실패든 그 사유로 보인다. "
                    "운영 조건 사유는 Burst.failures(...) 에, 그 밖의 불일치는 Burst.mismatches(...) 로 따로 (사유 없이 또는 기록 사유)"
                )
        for cid in sorted(set(case_rx.findall(f.content))):
            code = intended.get(cid)
            need = requires.get(code or "")
            if need and not any(re.search(p, f.content) for p in need):
                out.append(
                    f"숨은 케이스 {cid} (의도한 사유 {code}) 가 그 사유를 일으키는 것을 쓰지 않음 — 필요한 것: {' 또는 '.join(need)}. "
                    + (
                        "저장 단계 실패는 규칙을 통과하는 요청에 GraderJdbcHarness 주입으로 일으킨다 (거절되는 전이는 롤백될 변경이 없다)"
                        if "GraderJdbcHarness" in "".join(need)
                        else "운영 피크는 peak(...) 로 재현한다"
                    )
                )
    return out


# 상태를 바꾸는 요청(계약 API 함수 patch…·post…·put…·delete…)의 응답 — 그 응답을 담은 변수, 또는 그 호출 그대로
_MUTATING_VAR = re.compile(r"\b(\w+)\s*=\s*(?:patch|post|put|delete)[A-Z]\w*\s*\(")
_MUTATING_INLINE = re.compile(r"assertThat\(\s*(?:patch|post|put|delete)[A-Z]\w*\s*\(")
_RESET = re.compile(r"GraderJdbcHarness\.reset\(\)")
# 응답이 그 값(들)이어야 한다는 단언 — '성공 아님'(isNotEqualTo·isGreaterThanOrEqualTo…)은 그대로 둔다
_STATUS_UPPER = re.compile(r"\.(?:isLessThan|isLessThanOrEqualTo|isBetween|isStrictlyBetween)\(")
_STATUS_IS = re.compile(r"\.(?:isEqualTo|isIn|isOneOf)\(([^()]*)\)")


def _injected_status_asserts(
    cases: Iterable[GeneratedFile], m: TemplateManifest, contract: TaskContract
) -> list[str]:
    """저장 실패를 주입한 케이스가 응답 코드를 특정 값·범위로 단정함 — 계약은 저장 실패 때의 응답을 정하지 않는다.
    2026-11-09 알파 (2df170e2): hc2 가 커밋 실패 주입 뒤 `statusCode() < 500` 을 rollback_left_success 사유로 단언해,
    실패를 그대로 알린(500) 흔한 구현·허용 구현 모두 같은 줄에서 떨어졌다 (두 회차). 계약이 선언한 5xx 와 같다고 보는 것은 둔다."""
    inject = [
        re.compile(p)
        for r in m.failure_reasons
        if "GraderJdbcHarness" in "".join(r.requires)
        for p in r.requires
    ]
    if not inject:
        return []
    declared = {r.status for e in contract.endpoints for r in e.responses if r.status >= 500}
    out: list[str] = []
    for f in cases:
        start = min((mt.start() for rx in inject if (mt := rx.search(f.content))), default=None)
        if start is None:
            continue
        # 주입부터 reset 까지 보낸 상태 변경 요청만 — 그 뒤 기록을 읽는 GET 의 200 단언은 맞다
        # (2026-11-09 알파 재실행: 처음 검사가 주입 뒤 감사 조회 `auditResp.statusCode()).isEqualTo(200)` 까지 잡아 숨은 테스트 생성 실패)
        end = r.start() if (r := _RESET.search(f.content, start)) else len(f.content)
        window = f.content[start:end]
        injected = {mt.group(1) for mt in _MUTATING_VAR.finditer(window)}
        for stmt in window.split(";"):
            if "statusCode()" not in stmt or not (
                _MUTATING_INLINE.search(stmt)
                or any(
                    re.search(rf"assertThat\(\s*{re.escape(v)}\.statusCode\(\)\s*\)", stmt)
                    for v in injected
                )
            ):
                continue
            pinned = _STATUS_UPPER.search(stmt) or any(
                {int(v) for v in re.findall(r"\b\d{3}\b", mt.group(1))} - declared
                for mt in _STATUS_IS.finditer(stmt)
            )
            if pinned:
                out.append(
                    f"숨은 테스트 {f.path} 가 저장 실패를 주입한 요청의 응답 코드를 단정함: {' '.join(stmt.split())[:120]} — "
                    "계약은 저장 실패 때의 응답을 정하지 않아, 실패를 그대로 알린(5xx) 올바른 구현도 떨어진다. "
                    "응답은 보지 않거나 '성공이 아님'(isGreaterThanOrEqualTo(400))만 보고, 사유는 기록 단언에 붙인다"
                )
                break
    return out


def _hand_built_keys(
    cases: Sequence[GeneratedFile], contract: TaskContract, label: str
) -> list[str]:
    """도우미가 경로 키 값 함수(<필드>Of)를 주는데 테스트가 `"member-" + 행번호` 처럼 키 값을 직접 만듦 — 시드 값 형식을 짐작한다.
    2026-11-09 v16-be2: 시드 'memberId-191', 테스트 "member-" + HC2_FIRST → 모든 구현에서 대상 없음."""
    keys = [name for name, _, numeric in key_fields(contract) if not numeric]
    if not keys:
        return []
    rx = re.compile(r'"([A-Za-z][\w]*-)"\s*\+')
    out: list[str] = []
    for f in cases:
        built = sorted({m.group(1) for m in rx.finditer(f.content)})
        if built:
            out.append(
                f"{label} {f.path} 가 경로 키 값을 직접 만듦 ({', '.join(chr(34) + b + chr(34) + ' + …' for b in built)}) — "
                f"도우미의 {', '.join(k + 'Of(행 번호)' for k in keys)} 를 쓴다 (시드 값 형식을 짐작하지 않는다)"
            )
    return out


def _literal_headers(
    cases: Sequence[GeneratedFile], contract: TaskContract, label: str
) -> list[str]:
    """도우미에 헤더 값 상수가 있는데 테스트가 계약에 없는 값을 직접 씀 (`"X-Requester-Id", "grader"`) — 통과하는 값도
    거절되는 값도 아니면 그 요청의 결과를 계약이 정하지 않는다 (2026-11-08 알파: 지어낸 값으로 모든 요청이 403).
    거절 값 상수가 없으면 통과 값이 아닌 값은 거절 시험일 수 있어 둔다. 계약 값 그대로면 글자로 써도 막지 않는다."""
    out: list[str] = []
    for h in header_values(contract):
        if h.ok is None or h.denied is None:
            continue
        key = re.sub(r"[^A-Za-z0-9]+", "_", h.name).strip("_").upper()
        rx = re.compile(r'"' + re.escape(h.name) + r'"\s*,\s*"([^"]*)"', re.I)
        for f in cases:
            for mt in rx.finditer(f.content):
                v = mt.group(1)
                if v in (h.ok, h.denied):
                    continue
                out.append(
                    f'{label} {f.path} 가 헤더 {h.name} 값을 직접 씀 ("{v}") — 도우미의 HEADER_{key}_OK'
                    + f"·HEADER_{key}_DENIED 상수나 okHeaders() 를 쓴다 (계약이 정한 값)"
                )
    return out


def _hidden_cases(
    o: Overlay,
    t: Template,
    plan: HiddenPlan,
    contract: TaskContract | None = None,
    only: Collection[str] | None = None,
) -> list[StaticProblem]:
    """only: 케이스별 생성(generate.gen_hidden)에서 그 케이스만 — 계획의 다른 케이스가 없다고 하지 않는다."""
    m = t.manifest
    rules = m.hidden_rules
    out: list[str] = []
    imports = [re.compile(p, re.M) for p in rules.forbidden_imports]
    reason_rx = [re.compile(p) for p in rules.reason_patterns]
    case_rx = re.compile(rules.case_id_pattern)
    params_path = m.hidden_layout.params if m.hidden_layout else None
    helper = render_contract_case(contract, plan, m) if contract is not None else None
    helper_path = m.hidden_helper.path if m.hidden_helper else None
    plan_ids = {c.case_id for c in plan.cases}
    seen_ids: Counter[str] = Counter()
    used_codes: set[str] = set()
    for f in o.files:
        if f.path == params_path:
            if f.content != render_params(plan, m).content:
                out.append(
                    f"조절값 파일 {f.path} 를 손으로 씀 — 이 파일은 숨은 테스트에 넣지 않는다 (HiddenPlan 에서 코드가 쓴다)"
                )
            continue
        if f.path == helper_path:
            if helper is not None and f.content != helper.content:
                out.append(
                    f"계약 도우미 {f.path} 를 손으로 씀 — 이 파일은 숨은 테스트에 넣지 않는다 (계약에서 코드가 쓴다)"
                )
            continue
        if m.hidden_helper is not None and re.search(r"\bextends\s+GraderSupport\b", f.content):
            out.append(
                f"숨은 테스트 {f.path} 가 GraderSupport 를 직접 상속함 — {m.hidden_helper.class_name} 를 상속하고 그 범위 상수·계약 API 함수·"
                "peak() 를 쓴다 (id·경로·응답 파싱·시간 한도를 직접 쓰지 않는다)"
            )
        lines = f.content.splitlines()
        bad = [line.strip() for line in lines if any(rx.search(line) for rx in imports)]
        if bad:
            out.append(
                f"숨은 테스트 {f.path} 가 지원자 구현 내부를 직접 씀: {'; '.join(bad[:3])} — 공개 계약(HTTP)으로만 판정한다"
            )
        for rx in reason_rx:
            used_codes |= {mt.group(1) for mt in rx.finditer(f.content)}
        ids = set(case_rx.findall(f.content))
        seen_ids.update(ids)
        if only is not None and (others := sorted(ids & plan_ids - set(only))):
            out.append(
                f"숨은 테스트 {f.path} 에 다른 케이스 id 가 나옴: {', '.join(others)} — 이 파일은 {', '.join(sorted(only))} 만 다룬다"
                " (다른 케이스 id 는 주석·이름에도 쓰지 않는다 — 합치면 한 케이스가 여러 파일에 나온다)"
            )
    cases = [f for f in o.files if f.path not in (params_path, helper_path)]
    if helper is not None:
        out += unknown_helper_names(cases, helper, "숨은 테스트")
    out += unknown_harness_members(cases, t.hidden / "harness", "숨은 테스트")
    out += unknown_py_harness_members(cases, t.hidden / "harness", "숨은 테스트")
    out += unknown_py_keywords(cases, t.hidden / "harness", helper, "숨은 테스트")
    out += unrun_input_days(cases, "숨은 테스트")
    out += future_input_days(cases, "숨은 테스트")
    out += unreadable_as_pass(cases, "숨은 테스트")
    out += guessed_names(cases, contract, "숨은 테스트")
    out += type_escapes(cases, "숨은 테스트")
    out += _reason_rules(cases, m, plan, case_rx, reason_rx)
    if contract is not None and rules.http_calls:
        out += _unknown_calls(cases, contract, rules.http_calls)
    if contract is not None:
        out += _hidden_tables(cases, t, contract)
        out += _literal_headers(cases, contract, "숨은 테스트")
        out += _hand_built_keys(cases, contract, "숨은 테스트")
        out += _injected_status_asserts(cases, m, contract)
    if contract is not None and rules.raw_number_asserts:
        out += _raw_number_asserts(cases, rules.raw_number_asserts, contract)
    bad_codes = sorted(used_codes - m.reason_codes)
    if bad_codes:
        out.append(f"숨은 테스트의 실패 사유 코드가 템플릿 목록 밖: {', '.join(bad_codes)}")
    planned = {i for i in plan_ids if only is None or i in only}
    missing = sorted(planned - set(seen_ids))
    extra = sorted(set(seen_ids) - plan_ids)
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


_HELPER_CONST = re.compile(
    r"\b(HC\d+(?:_[A-Z0-9]+)*_(?:FIRST|LAST|ROWS)|PUBLIC_[A-Z0-9_]+|TABLE_[A-Z0-9_]+|NEXT_(?:OK|BAD)_[A-Z0-9_]+)\b"
)
_VERB_CALL = re.compile(r"(?<![\w.])((?:get|post|put|patch|delete)[A-Z]\w*)\s*\(")
_OWN_METHOD = re.compile(r"[\w>\]]\s+(\w+)\s*\([^;{}]*\)\s*(?:throws\s+[\w., ]+)?\s*\{")
_OWN_FIELD = re.compile(r"\b([A-Z][A-Z0-9_]+)\s*=")
_JDK_VERB = {"getClass"}


def helper_names(helper: GeneratedFile) -> set[str]:
    """코드가 쓴 도우미(ContractCase·PublicCase)의 함수·상수 이름."""
    methods = re.findall(r"protected (?:static )?[\w<>, \[\]]+? (\w+)\(", helper.content)
    consts = re.findall(r"protected static final [\w\[\]]+ (\w+) =", helper.content)
    return set(methods) | set(consts)


_ROLE_NAME = re.compile(r"""ByRole\(\s*['"]([a-z]+)['"]\s*,\s*\{[^}]*\bname\s*:\s*([^,}]+)""")


_TYPE_ESCAPE = re.compile(
    r"\bas\s+unknown\s+as\b|\bas\s+any\b|:\s*any\b|@ts-(?:ignore|expect-error|nocheck)"
)


def type_escapes(cases: Iterable[GeneratedFile], label: str) -> list[str]:
    """TypeScript 테스트가 타입 검사를 우회 (`as unknown as`·`as any`·`: any`·`@ts-ignore`) — 계약·하네스 타입이 잡아 주는 잘못(없는 필드·
    다른 응답 모양)이 실행 때 '요청이 오지 않음'·빈 화면으로 숨는다. 2026-11-09 React 알파(낙관적 갱신): 컴파일 루프가 'Call 에 json 이 없다'고
    알리자 as unknown as 로 우회해, 없는 json 필드를 보는 조건 때문에 모든 구현에서 요청을 못 찾았다 (실제 필드는 body)."""
    out: list[str] = []
    for f in cases:
        if not f.path.endswith((".ts", ".tsx")):
            continue
        hits = sorted({m.group(0) for m in _TYPE_ESCAPE.finditer(f.content)})
        if hits:
            out.append(
                f"{label} {f.path} 가 타입 검사를 우회함 ({', '.join(hits)}) — 하네스·계약 도우미의 타입대로 쓴다 "
                "(요청 기록은 call.query·call.params·call.body, 응답 본문은 <Api>Ok 타입)"
            )
    return out


def guessed_names(
    cases: Iterable[GeneratedFile], contract: TaskContract | None, label: str
) -> list[str]:
    """화면 계약(screens)이 있는 과제에서 테스트가 요소를 접근 가능한 이름으로 찾음 — 계약에는 이름이 없어(test id·역할·설명만)
    짐작한 이름이고, 올바른 구현도 못 찾는다. 2026-11-09 React 알파: 하네스 문서 예시의 '상품 검색'을 따라 써 모든 구현에서 예외."""
    if contract is None or not contract.screens:
        return []
    out: list[str] = []
    for f in cases:
        hits = sorted(
            {f"{role} {name.strip()[:30]}" for role, name in _ROLE_NAME.findall(f.content)}
        )
        if hits:
            out.append(
                f"{label} {f.path} 가 요소를 접근 가능한 이름으로 찾음 ({'; '.join(hits[:3])}) — 계약에 이름이 없다. "
                "계약의 test id 로 찾는다: await screen.findByTestId(TID.X) · byTestId(TID.X)"
            )
    return out


_PY_CLASS = re.compile(r"^class (\w+)\b[^\n]*:\n((?:(?:[ \t]+[^\n]*|)\n)*)", re.M)


@functools.lru_cache(maxsize=8)
def py_harness_members(grader: Path) -> dict[str, frozenset[str]]:
    """Python 하네스(grader.py)의 클래스 → 쓸 수 있는 이름 (메서드·속성·필드). Workspace·JobResult 를 테스트가 쓴다."""
    if not grader.is_file():
        return {}
    src = grader.read_text("utf-8")
    out: dict[str, frozenset[str]] = {}
    for m in _PY_CLASS.finditer(src):
        body = m.group(2)
        names = set(re.findall(r"^\s{4}def (\w+)\(", body, re.M))
        names |= set(re.findall(r"^\s{4}(\w+)\s*:", body, re.M))  # 데이터클래스 필드
        names |= set(re.findall(r"self\.(\w+)\s*=", body))
        out[m.group(1)] = frozenset(n for n in names if not n.startswith("_"))  # 내부용 이름은 빼고
    return out


_PY_MAKERS = {"Workspace": r"workspace\(\)", "JobResult": r"run(?:_\w+)?\("}


def unknown_py_harness_members(
    cases: Iterable[GeneratedFile], harness_dir: Path, label: str
) -> list[str]:
    """Python 테스트가 하네스 객체(workspace() 의 Workspace, run…() 의 JobResult)에 없는 속성을 씀 — 실행해야 드러나는 AttributeError.
    2026-11-09 데이터 알파: 공개 테스트가 ws.input·ws.output·ws.extend 를 지어내 모든 구현에서 예외."""
    members = py_harness_members(harness_dir / "grader.py")
    if not members:
        return []
    out: list[str] = []
    for f in cases:
        if not f.path.endswith(".py"):
            continue
        bad: set[tuple[str, str, str]] = set()
        for cls, maker in _PY_MAKERS.items():
            allowed = members.get(cls)
            if not allowed:
                continue
            for var in set(re.findall(rf"^\s*(\w+)\s*=\s*{maker}", f.content, re.M)):
                for attr in re.findall(rf"(?<![\w.]){re.escape(var)}\.(\w+)", f.content):
                    if attr not in allowed:
                        bad.add((var, cls, attr))
        if bad:
            hints = []
            for var, cls, attr in sorted(bad):
                near = difflib.get_close_matches(attr, sorted(members[cls]), n=1, cutoff=0.5)
                hints.append(
                    f"{var}.{attr} ({cls}" + (f" — 가까운 이름: {near[0]})" if near else ")")
                )
            out.append(
                f"{label} {f.path} 가 하네스에 없는 이름을 씀: {', '.join(hints)} — 하네스 문서의 이름만 쓴다 "
                f"(Workspace: {', '.join(sorted(members.get('Workspace', ())))})"
            )
    return out


@dataclass(frozen=True)
class PySignature:
    """Python 함수·메서드 시그니처 — 테스트가 하네스·계약 도우미를 맞게 부르는지 (키워드 이름·필수 인자) 대조용."""

    positional: tuple[str, ...]  # 위치로 받을 수 있는 이름 (순서대로, self 제외)
    keywords: frozenset[str]  # 키워드로 받을 수 있는 이름
    required: frozenset[str]  # 기본값 없는 이름
    varargs: bool  # *args
    varkw: bool  # **kwargs
    returns: str  # 돌려주는 클래스 이름 (모르면 "")


def _signature_of(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> PySignature:
    a = fn.args
    pos = [x.arg for x in [*a.posonlyargs, *a.args] if x.arg not in ("self", "cls")]
    kw_pos = [x.arg for x in a.args if x.arg not in ("self", "cls")]
    n_def = len(a.defaults)
    pos_required = pos[: len(pos) - n_def] if n_def else pos
    kwonly_required = [x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is None]
    ret = ""
    if isinstance(fn.returns, ast.Name):
        ret = fn.returns.id
    elif isinstance(fn.returns, ast.Constant) and isinstance(fn.returns.value, str):
        ret = fn.returns.value  # 문자열 표기
    return PySignature(
        positional=tuple(pos),
        keywords=frozenset([*kw_pos, *(x.arg for x in a.kwonlyargs)]),
        required=frozenset([*pos_required, *kwonly_required]),
        varargs=a.vararg is not None,
        varkw=a.kwarg is not None,
        returns=ret,
    )


def _py_signatures(src: str) -> tuple[dict[str, PySignature], dict[str, str]]:
    """Python 모듈 → 함수·메서드 시그니처('f' · 'Class.m')와 모듈 수준 인스턴스(`params = _Params()` → 클래스)."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}, {}
    sigs: dict[str, PySignature] = {}
    instances: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sigs[node.name] = _signature_of(node)
        elif isinstance(node, ast.ClassDef):
            for b in node.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sigs[f"{node.name}.{b.name}"] = _signature_of(b)
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    instances[tgt.id] = node.value.func.id
    return sigs, instances


@functools.lru_cache(maxsize=8)
def _harness_signatures(grader: Path) -> tuple[dict[str, PySignature], dict[str, str]]:
    return _py_signatures(grader.read_text("utf-8")) if grader.is_file() else ({}, {})


def _call_problems(node: ast.Call, name: str, sig: PySignature) -> list[str]:
    """없는 키워드 이름·빠진 필수 인자 (*args·**kwargs 를 받거나 넘기면 보지 않는다)."""
    if any(isinstance(a, ast.Starred) for a in node.args) or any(
        k.arg is None for k in node.keywords
    ):
        return []
    out: list[str] = []
    accepted = ", ".join(sig.positional + tuple(sorted(sig.keywords - set(sig.positional))))
    if not sig.varkw:
        out += [
            f"{name}({k.arg}=…) — 받는 이름: {accepted or '없음'}"
            for k in node.keywords
            if k.arg is not None and k.arg not in sig.keywords
        ]
    if not sig.varargs:
        given = set(sig.positional[: len(node.args)]) | {k.arg for k in node.keywords if k.arg}
        missing = sorted(sig.required - given)
        if missing:
            out.append(
                f"{name}(…) 에 필수 인자 {', '.join(missing)} 가 빠짐 — 받는 이름: {accepted}"
            )
    return out


def unknown_py_keywords(
    cases: Iterable[GeneratedFile], harness_dir: Path, helper: GeneratedFile | None, label: str
) -> list[str]:
    """Python 테스트가 하네스 함수·메서드(workspace().write_csv 등)나 계약 도우미 함수(run_…)를 시그니처와 다르게 부름 — 실행해야 드러나는
    TypeError. 없는 키워드 인자·빠진 필수 인자·문맥 관리자가 아닌 하네스 객체에 with·작업 폴더 경로를 문자열 그대로 명령에(5회차).
    2026-11-09 데이터 알파 2~3회차: 공개 테스트가 ws.write_csv(..., header=...)·ws.partitions(폴더)(key 빠짐)·`with workspace() as ws`
    를 지어내 모든 구현에서 예외 (다시 만들어도 반복)."""
    h_sigs, h_inst = _harness_signatures(harness_dir / "grader.py")
    c_sigs, _ = _py_signatures(helper.content) if helper is not None else ({}, {})
    if not h_sigs and not c_sigs:
        return []
    out: list[str] = []
    for f in cases:
        if not f.path.endswith(".py"):
            continue
        try:
            tree = ast.parse(f.content)
        except SyntaxError:
            continue  # 문법 오류는 컴파일 확인이 알린다
        funcs: dict[str, PySignature] = {}  # 이 파일이 import 한 하네스·도우미 함수 (별칭 포함)
        commands: set[str] = set()  # 그중 계약 도우미의 명령 함수 (run_…)
        objs: dict[str, str] = {}  # 이름 → 하네스 클래스 (params 같은 인스턴스, workspace() 결과)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                last = node.module.rsplit(".", 1)[-1]
                pool = (
                    h_sigs
                    if node.module == "harness.grader"
                    else c_sigs
                    if last == "contract"
                    else None
                )
                if pool is None:
                    continue
                for al in node.names:
                    local = al.asname or al.name
                    if al.name in pool:
                        funcs[local] = pool[al.name]
                        if pool is c_sigs:
                            commands.add(local)
                    elif pool is h_sigs and al.name in h_inst:
                        objs[local] = h_inst[al.name]

        def made_by(value: ast.expr | None, funcs: dict[str, PySignature] = funcs) -> str:
            """하네스·도우미 함수 호출 결과의 클래스 (모르면 "")."""
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in funcs
            ):
                return funcs[value.func.id].returns
            return ""

        bad: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and (cls := made_by(node.value)):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        objs[tgt.id] = cls
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    expr = item.context_expr
                    cls = made_by(expr) or (
                        objs.get(expr.id, "") if isinstance(expr, ast.Name) else ""
                    )
                    if cls and f"{cls}.__enter__" not in h_sigs:
                        bad.append(
                            f"with {ast.unparse(expr)} — {cls} 는 with 로 쓰지 않는다 (그냥 대입: x = {ast.unparse(expr)})"
                        )
                        if isinstance(item.optional_vars, ast.Name):
                            objs[item.optional_vars.id] = cls
        # 작업 폴더에 쓰고 읽는 경로의 첫 마디 (ws.write_csv("raw/…") → raw) — 명령에 그 이름을 문자열 그대로 넘기면 저장소 루트 기준이 된다
        ws_roots: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in objs
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                first = node.args[0].value.strip("/").removeprefix("./").split("/", 1)[0]
                if first:
                    ws_roots.add(first)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in funcs:
                bad += _call_problems(node, node.func.id, funcs[node.func.id])
                if node.func.id in commands:
                    for kw in node.keywords:
                        v = kw.value
                        if (
                            kw.arg
                            and isinstance(v, ast.Constant)
                            and isinstance(v.value, str)
                            and v.value.removeprefix("./").split("/", 1)[0] in ws_roots
                        ):
                            bad.append(
                                f"{node.func.id}({kw.arg}={v.value!r}) — 작업 폴더 경로를 문자열 그대로 넘김. 명령은 저장소 루트에서 돌므로 "
                                f"ws.path({v.value!r}) 로 넘긴다"
                            )
            elif (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in objs
            ):
                cls = objs[node.func.value.id]
                sig = h_sigs.get(f"{cls}.{node.func.attr}")
                if sig is not None:
                    bad += _call_problems(node, f"{node.func.value.id}.{node.func.attr}", sig)
        if bad:
            out.append(
                f"{label} {f.path} 가 하네스·계약 도우미를 잘못 부름: {'; '.join(sorted(set(bad))[:6])} — "
                "하네스 문서·계약 도우미의 시그니처대로, 경로는 ws.path(…) 로 넘긴다"
            )
    return out


_DAY = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_WRITERS = frozenset({"write_jsonl", "write_csv", "write_parquet", "write_text", "write_json"})


def unrun_input_days(cases: Iterable[GeneratedFile], label: str) -> list[str]:
    """Python 테스트 함수가 날짜 폴더(`raw/2026-09-01/…`)에 입력을 쓰고 그 날짜로는 명령을 실행하지 않음 — 마지막 날짜로 한 번만 실행해 앞 날짜
    출력까지 기대하는 꼴. 받은 날짜마다 실행하는 정상 운영에서만 맞는 허용 설계('이번 폴더에 나온 날짜만 다시 계산')가 떨어진다.
    2026-11-09 데이터 알파 2~8회차 재검증: 손으로 쓴 허용 구현 두 가지로 돌려 보니 이 꼴의 케이스(hc3·hc4)가 7회 중 6회 나왔다.
    날짜·경로가 변수로 만들어지는 함수는 보지 않는다 (글자 그대로인 것만)."""
    out: list[str] = []
    for f in cases:
        if not f.path.endswith(".py"):
            continue
        try:
            tree = ast.parse(f.content)
        except SyntaxError:
            continue
        commands: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").rsplit(".", 1)[-1] == "contract"
            ):
                commands |= {
                    al.asname or al.name for al in node.names if al.name.startswith("run_")
                }
        if not commands:
            continue
        for fn in tree.body:
            if not (
                isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                and fn.name.startswith("test")
            ):
                continue
            written: set[str] = set()
            ran: set[str] = set()
            unknown = False
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in _WRITERS
                    and node.args
                ):
                    a0 = node.args[0]
                    if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                        written |= {
                            m.group(1)
                            for m in _DAY.finditer(a0.value)
                            if f"{m.group(1)}/" in a0.value
                        }
                    else:
                        unknown = True
                elif isinstance(node.func, ast.Name) and node.func.id in commands:
                    for kw in node.keywords:
                        v = kw.value
                        if (
                            isinstance(v, ast.Constant)
                            and isinstance(v.value, str)
                            and _DAY.fullmatch(v.value)
                        ):
                            ran.add(v.value)
                        elif kw.arg and "date" in kw.arg and not isinstance(v, ast.Constant):
                            unknown = True
            missing = sorted(written - ran)
            if ran and missing and not unknown:
                out.append(
                    f"{label} {f.path} 의 {fn.name} 가 날짜 폴더 {', '.join(missing)} 에 입력을 쓰고 그 날짜로 실행하지 않음 (실행한 날짜: "
                    f"{', '.join(sorted(ran))}) — 받은 날짜 순서대로 하루씩 실행한다. 마지막 날짜로 한 번만 실행해 앞 날짜 출력까지 기대하면 "
                    "'이번 폴더에 나온 날짜만 다시 계산'하는 허용 설계가 떨어진다"
                )
    return out


def future_input_days(cases: Iterable[GeneratedFile], label: str) -> list[str]:
    """Python 테스트 함수가 실행 날짜보다 **뒤** 날짜 폴더를 그 실행 전에 미리 씀 — 실제로는 아직 받지 않은 입력이라, 공개 계약에 없는 요구
    ('실행 날짜보다 뒤 폴더는 읽지 않는다')를 시험하게 된다. 2026-11-10 데이터 알파 10회차: hc1·hc2 가 9/2 폴더를 9/1 실행 전에 써 두어,
    받은 폴더를 모두 읽는 허용 구현(손 구현으로 재현)이 떨어졌다. 줄 순서로 본다 — 날짜·경로가 변수인 함수는 보지 않는다."""
    out: list[str] = []
    for f in cases:
        if not f.path.endswith(".py"):
            continue
        try:
            tree = ast.parse(f.content)
        except SyntaxError:
            continue
        commands: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").rsplit(".", 1)[-1] == "contract"
            ):
                commands |= {
                    al.asname or al.name for al in node.names if al.name.startswith("run_")
                }
        if not commands:
            continue
        for fn in tree.body:
            if not (
                isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                and fn.name.startswith("test")
            ):
                continue
            events: list[tuple[int, str, str]] = []  # (줄, write|run, 날짜)
            unknown = False
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in _WRITERS
                    and node.args
                ):
                    a0 = node.args[0]
                    if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                        events += [
                            (node.lineno, "write", m.group(1))
                            for m in _DAY.finditer(a0.value)
                            if f"{m.group(1)}/" in a0.value
                        ]
                    else:
                        unknown = True
                elif isinstance(node.func, ast.Name) and node.func.id in commands:
                    for kw in node.keywords:
                        v = kw.value
                        if (
                            isinstance(v, ast.Constant)
                            and isinstance(v.value, str)
                            and _DAY.fullmatch(v.value)
                        ):
                            events.append((node.lineno, "run", v.value))
                        elif kw.arg and "date" in kw.arg and not isinstance(v, ast.Constant):
                            unknown = True
            if unknown:
                continue
            if any(isinstance(n, (ast.For, ast.While)) for n in ast.walk(fn)):
                continue  # 반복문 안은 줄 순서가 실행 순서가 아니다
            written: set[str] = set()
            early: set[tuple[str, str]] = set()
            for _, kind, day in sorted(events):
                if kind == "write":
                    written.add(day)
                else:
                    early |= {(w, day) for w in written if w > day}
            if early:
                pairs = ", ".join(f"{w}(실행 {r} 전)" for w, r in sorted(early)[:4])
                out.append(
                    f"{label} {f.path} 의 {fn.name} 가 실행 날짜보다 뒤 날짜 폴더를 그 실행 전에 씀: {pairs} — 입력은 받은 순서대로, "
                    "날짜 폴더는 그 날짜를 실행하기 직전에 쓴다. 미리 두면 공개 계약에 없는 '뒤 폴더는 읽지 않는다'를 시험하게 되어 "
                    "받은 폴더를 모두 읽는 허용 구현이 떨어진다"
                )
    return out


def _compares(expr: ast.expr, values: set[str], op: type[ast.cmpop]) -> bool:
    return (
        isinstance(expr, ast.Compare)
        and len(expr.ops) == 1
        and isinstance(expr.ops[0], op)
        and any(
            isinstance(x, ast.Constant) and x.value in values
            for x in (expr.left, *expr.comparators)
        )
    )


def unreadable_as_pass(cases: Iterable[GeneratedFile], label: str) -> list[str]:
    """크래시 케이스가 `except` 에서 바꾼 '읽을 수 없음' 표시 값을 통과로 둠 — `if v != 'unreadable': assert …`(그 값이면 단언을 건너뜀)이나
    `assert v == 'UNREADABLE' or …`. 깨진 파일이 보이는 것이 바로 숨은 요구 위반인데 최종 위치에 바로 쓰다 죽는 흔한 구현이 통과한다.
    2026-11-10 데이터 알파 8·9회차: 두 번 다 이 꼴 (프롬프트의 '읽을 수 없음을 값으로 바꿔 단언'이 무엇이 실패인지 말하지 않았다)."""
    out: list[str] = []
    for f in cases:
        if not f.path.endswith(".py"):
            continue
        try:
            tree = ast.parse(f.content)
        except SyntaxError:
            continue
        marks: set[str] = set()
        for h in ast.walk(tree):
            if isinstance(h, ast.ExceptHandler):
                for n in ast.walk(h):
                    v = n.value if isinstance(n, (ast.Return, ast.Assign)) else None
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        marks.add(v.value)
        if not marks:
            continue
        hits: list[int] = []
        for n in ast.walk(tree):
            if (
                isinstance(n, ast.If)
                and _compares(n.test, marks, ast.NotEq)
                and any(isinstance(x, ast.Assert) for b in n.body for x in ast.walk(b))
            ):
                hits.append(n.lineno)
            elif (
                isinstance(n, ast.Assert)
                and isinstance(n.test, ast.BoolOp)
                and isinstance(n.test.op, ast.Or)
                and any(_compares(v, marks, ast.Eq) for v in n.test.values)
            ):
                hits.append(n.lineno)
        if hits:
            out.append(
                f"{label} {f.path} 가 크래시 뒤 '읽을 수 없음'({', '.join(sorted(repr(m) for m in marks))})을 통과로 둠 (줄 {', '.join(map(str, sorted(hits)[:4]))}) — "
                "읽을 수 없는 출력이 보이면 그것이 실패다. 그 값을 건너뛰거나 허용하지 말고 직전 성공 값(또는 성공했을 때의 값)과만 같게 단언한다"
            )
    return out


def unknown_helper_names(
    cases: Iterable[GeneratedFile], helper: GeneratedFile, label: str
) -> list[str]:
    """케이스가 도우미에 없는 범위·공개 행·테이블 상수나 계약 API 함수를 쓰면 가까운 실제 이름과 함께.
    2026-11-08 상류 평가: 도우미 소스를 보여 줬는데도 getApiOrdersOrderIdStatus(실제 …StatusAudit)·PUBLIC_ORDER_READY(없는 처음 값)를
    짐작해 컴파일 실패 — 이름은 코드가 만들어 정확히 알므로 Gradle 전에 대조한다."""
    if not helper.path.endswith(
        ".java"
    ):  # 이름 꼴이 Java 기준 — TypeScript 도우미(contract.ts)의 없는 이름은 tsc 가 잡는다
        return []
    known = helper_names(helper)
    out: list[str] = []
    for f in cases:
        own = set(_OWN_METHOD.findall(f.content)) | set(_OWN_FIELD.findall(f.content))
        used = set(_HELPER_CONST.findall(f.content)) | set(_VERB_CALL.findall(f.content))
        bad = sorted(u for u in used if u not in known and u not in own and u not in _JDK_VERB)
        if not bad:
            continue
        hints = []
        for b in bad:
            near = difflib.get_close_matches(b, sorted(known), n=1, cutoff=0.6)
            family = sorted(k for k in known if k.split("_")[0] == b.split("_")[0] and k.isupper())
            if near:
                hints.append(f"{b} (가까운 이름: {near[0]})")
            elif family and b.isupper():
                hints.append(f"{b} (있는 이름: {', '.join(family[:6])})")
            else:
                hints.append(b)
        out.append(
            f"{label} {f.path} 가 도우미에 없는 이름을 씀: {', '.join(hints)} — 도우미 소스에 있는 상수·함수 이름 그대로 쓴다 "
            "(없는 이름을 짐작하지 않는다)"
        )
    return out


_STATIC_MEMBER = re.compile(
    r"(?:public|protected)\s+static\s+(?:(?:final|volatile|synchronized)\s+)*[\w<>\[\],.? ]+?\s+(\w+)\s*[(=;]"
)
_NESTED_TYPE = re.compile(
    r"(?:public|protected)\s+(?:(?:static|final|abstract|sealed)\s+)*(?:record|interface|class|enum)\s+(\w+)"
)


_JAVA_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def _strip_comments(src: str) -> str:
    return _JAVA_COMMENT.sub(" ", src)


@functools.lru_cache(maxsize=8)
def harness_members(harness_dir: Path) -> dict[str, frozenset[str]]:
    """잠긴 하네스 클래스 → 클래스 이름으로 부를 수 있는 것 (static 메서드·필드, 중첩 타입). 템플릿 소스에서 읽는다."""
    out: dict[str, frozenset[str]] = {}
    for f in sorted(harness_dir.glob("*.java")) if harness_dir.is_dir() else []:
        src = f.read_text(encoding="utf-8")
        nested = {n for n in _NESTED_TYPE.findall(src) if n != f.stem}
        out[f.stem] = frozenset(set(_STATIC_MEMBER.findall(src)) | nested | {"class"})
    return out


def unknown_harness_members(
    cases: Iterable[GeneratedFile], harness_dir: Path, label: str
) -> list[str]:
    """케이스가 하네스 클래스에 없는 멤버를 부르면 (`Burst.IntUnaryOperator` 등) 가까운 실제 이름과 함께.
    2026-11-08 상류 평가: 하네스 문서에 `IntUnaryOperator expectedStatus` 라 적혀 있는데 `Burst.IntUnaryOperator` 로 써 컴파일 실패."""
    members = harness_members(harness_dir)
    if not members:
        return []
    rx = re.compile(rf"(?<![\w.])({'|'.join(map(re.escape, members))})\.([A-Za-z_]\w*)")
    out: list[str] = []
    for f in cases:
        bad = sorted(
            {(c, n) for c, n in rx.findall(_strip_comments(f.content)) if n not in members[c]}
        )
        if not bad:
            continue
        hints = []
        for c, n in bad:
            near = difflib.get_close_matches(n, sorted(members[c] - {"class"}), n=1, cutoff=0.6)
            if near:
                hints.append(f"{c}.{n} (가까운 이름: {c}.{near[0]})")
            elif n[:1].isupper():
                hints.append(f"{c}.{n} ({c} 안의 타입이 아님 — JDK 타입이면 import 해서 그냥 {n})")
            else:
                hints.append(f"{c}.{n}")
        out.append(
            f"{label} {f.path} 가 하네스에 없는 멤버를 씀: {', '.join(hints)} — 하네스 문서·소스에 있는 이름 그대로 쓴다"
        )
    return out


_HIDDEN_ONLY = re.compile(r"\b(?:Burst|GraderJdbcHarness|GraderParams)\.|\bpeak\(")


def _public_cases(
    o: Overlay, t: Template, plan: HiddenPlan, contract: TaskContract | None
) -> list[StaticProblem]:
    """공개 테스트(gen_public) — 코드가 쓴 도우미·저장 확인은 그대로, 케이스는 PublicCase 상속·계약 API 만·pt 마다 하나·숨은 요구 영역 금지."""
    m = t.manifest
    rule = m.public_helper
    if rule is None or contract is None:
        return []
    written = {
        f.path: f
        for f in (
            render_contract_case(contract, plan, m, audience="public"),
            render_persistence_check(contract, m),
        )
        if f is not None
    }
    out: list[str] = []
    cases: list[GeneratedFile] = []
    for f in o.files:
        if f.path in written:
            if f.content != written[f.path].content:
                out.append(
                    f"{f.path} 를 손으로 씀 — 이 파일은 계약에서 코드가 쓴다 (공개 테스트에 넣지 않는다)"
                )
            continue
        cases.append(f)
        # 상속·숨은 전용 이름 검사는 Java 하네스 기준 — TypeScript 는 tsc 컴파일 루프와 import 규칙으로
        if not is_java(m):
            continue
        if not re.search(rf"\bextends\s+{re.escape(rule.class_name)}\b", f.content):
            out.append(
                f"공개 테스트 {f.path} 가 {rule.class_name} 를 상속하지 않음 — {rule.class_name} 의 공개 몫 행 상수·계약 API 함수·Body 함수를 쓴다"
            )
        if _HIDDEN_ONLY.search(f.content):
            out.append(
                f"공개 테스트 {f.path} 가 동시 요청·오류 주입·조절값을 씀 — 숨은 요구 영역이다. 공개 수용 기준(응답·기록)만 확인한다"
            )
    out += _literal_headers(cases, contract, "공개 테스트")
    out += _hand_built_keys(cases, contract, "공개 테스트")
    imports = [re.compile(p, re.M) for p in m.hidden_rules.forbidden_imports]
    for f in cases:
        bad = [
            line.strip()
            for line in f.content.splitlines()
            if any(rx.search(line) for rx in imports)
        ]
        if bad:
            out.append(
                f"공개 테스트 {f.path} 가 지원자 구현 내부를 직접 씀: {'; '.join(bad[:3])} — 공개 계약(HTTP)으로만 판정한다"
            )
    if m.hidden_rules.http_calls:
        out += [
            msg.replace("숨은 테스트", "공개 테스트")
            for msg in _unknown_calls(cases, contract, m.hidden_rules.http_calls)
        ]
    helper = written.get(rule.path)
    if helper is not None:
        out += unknown_helper_names(cases, helper, "공개 테스트")
    out += unknown_harness_members(cases, t.hidden / "harness", "공개 테스트")
    out += unknown_py_harness_members(cases, t.hidden / "harness", "공개 테스트")
    out += unknown_py_keywords(cases, t.hidden / "harness", helper, "공개 테스트")
    out += unrun_input_days(cases, "공개 테스트")
    out += future_input_days(cases, "공개 테스트")
    out += unreadable_as_pass(cases, "공개 테스트")
    out += guessed_names(cases, contract, "공개 테스트")
    out += type_escapes(cases, "공개 테스트")
    text = "\n".join(f.content for f in cases)
    missing = [
        pt.test_id for pt in contract.public_tests if not re.search(rf"\b{pt.test_id}\b", text)
    ]
    if missing:
        out.append(
            f"계약의 공개 테스트 {', '.join(missing)} 가 공개 테스트에 없음 — pt 마다 클래스 하나, @DisplayName 에 pt id"
        )
    return [StaticProblem("gen_public", msg) for msg in out]


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
    hidden_cases_only: Collection[str] | None = None,
) -> StaticResult:
    """오버레이 정적 검사 (§6). problems 는 생성기별 재생성 사유, review_flags 는 승인자에게."""
    res = StaticResult()
    hidden_files = [f for o in overlays if o.kind == "hidden" for f in o.files]
    # 코드가 쓴 파일(조절값·계약 도우미)의 이름은 빼고 — 2026-11-09 React 알파: 숨은 도우미 contract.ts 의 이름이 공개 테스트의
    # `import … from './contract'`(공개 도우미)를 누설로 막아 공개 테스트 생성이 재생성 예산을 다 썼다
    m_ = template.manifest
    written = {m_.hidden_layout.params if m_.hidden_layout else None}
    written |= {m_.hidden_helper.path if m_.hidden_helper else None}
    hidden_names = [PurePosixPath(f.path).stem for f in hidden_files if f.path not in written]
    terms = list(
        dict.fromkeys([*hidden_terms(seed, spec), *(n for n in hidden_names if len(n) >= 4)])
    )

    for o in overlays:
        res.problems += _paths(o, template)
        res.problems += [StaticProblem(o.target, msg) for msg in _hygiene(o, o.files, avoid_terms)]
        # 공개 테스트는 지원자에게 주지 않아도 숨은 요구 낱말이 있으면 안 된다 (게이트 ⑤ 누설 사전 확인)
        public = o.kind in ("starter", "public_test")
        if public:
            for path, shown in _leaks(o.files, terms):
                res.problems.append(
                    StaticProblem(
                        o.target,
                        f"공개 파일 {path} 에 숨은 요구·운영 조건·숨은 테스트를 드러내는 말: {', '.join(shown)} "
                        "— 코드·주석·이름에 쓰지 않는다 (미끼 코드 포함)",
                    )
                )
        if o.kind in ("starter", "ref_naive", "ref_accepted"):
            res.problems += _unavailable_imports(o, template)
            res.problems += _test_folder_imports(o, template)
            res.problems += _code_overrides(o, template)
            res.problems += _code_pitfalls(o, template)
        if o.kind == "starter":
            problems, flags = _operating_values(o, template, plan)
            res.problems += problems
            res.problems += _starter_tables(o, template, contract)
            res.problems += _starter_foreign_keys(o, template, contract)
            res.problems += _starter_new_api_tests(o, template, contract, spec)
            res.problems += _starter_schema(o, template, contract)
            res.review_flags += flags
        if o.kind == "public_test":
            res.problems += _public_cases(o, template, plan, contract)
        if o.kind == "hidden":
            res.problems += _hidden_cases(o, template, plan, contract, hidden_cases_only)
            res.review_flags += _duplicate_labels(o, template)
        if o.kind == "ref_accepted":
            starter = next((x for x in overlays if x.kind == "starter"), None)
            res.problems += _harness_overrides(o, starter, template, plan)

    if readme is not None:
        # README 는 승인된 명세 문장으로 결정적으로 만든다 — 걸리면 명세 문제 (재생성으로 못 고침)
        for _, shown in _leaks([readme], hidden_terms(seed, spec)):
            res.review_flags.append(
                f"{README} 에 숨은 요구·운영 조건을 드러내는 말: {', '.join(shown)} — 승인된 명세 문장에서 나옴, 명세 수정 필요"
            )
        res.review_flags += _hygiene(None, [readme], avoid_terms)
    res.review_flags = list(dict.fromkeys(res.review_flags))
    return res


def _code_pitfalls(o: Overlay, t: Template) -> list[StaticProblem]:
    """manifest code_pitfalls — 이 스택에서 생성 모델이 되풀이하는 API 실수를 실행 전에 (파일·줄과 바른 쓰는 법)."""
    out: list[StaticProblem] = []
    for pit in t.manifest.code_pitfalls:
        rx = re.compile(pit.pattern)
        ok = re.compile(pit.unless) if pit.unless else None
        hits = [
            f"{f.path}:{f.content.count(chr(10), 0, m.start()) + 1} `{' '.join(m.group(0).split())[:80]}`"
            for f in o.files
            if ok is None or not ok.search(f.content)
            for m in rx.finditer(f.content)
        ]
        if hits:
            out.append(
                StaticProblem(
                    o.target,
                    f"{o.name} 에 이 스택에서 되풀이되는 실수: {'; '.join(hits[:4])} — {pit.fix}",
                )
            )
    return out


def overlay_from_dir(kind: FileKind, root: Path, label: str = "") -> Overlay:
    """폴더 안 파일 → Overlay (평가·테스트용). 경로는 root 기준."""
    files = tuple(
        GeneratedFile(path=p.relative_to(root).as_posix(), content=p.read_text("utf-8"))
        for p in sorted(root.rglob("*"))
        if p.is_file()
    )
    return Overlay(kind=kind, files=files, label=label)
