"""Java 테스트의 빠진 JDK import 를 코드가 채운다 — TypeScript 의 ensure_vitest_imports 와 같은 몫.

2026-11-10 v19-be3: 숨은 테스트가 도우미(ContractCase)의 반환형 `HttpResponse<String>` 을 쓰면서
`import java.net.http.HttpResponse;` 를 빠뜨려, 재요청 3번에도 같은 'cannot find symbol' 로 루프가 멈췄다.
고칠 것이 정해져 있으니 생성기에 다시 묻지 않는다. JDK 표준 이름만 — 프로젝트 클래스는 짐작하지 않는다.
"""

from __future__ import annotations

import re

from task_spec.schemas import GeneratedFile

# 테스트가 흔히 쓰는 JDK 이름 → 패키지 (같은 이름의 다른 클래스를 import 하거나 정의하면 건드리지 않는다)
JDK_TYPES: dict[str, str] = {
    "HttpResponse": "java.net.http",
    "HttpRequest": "java.net.http",
    "HttpClient": "java.net.http",
    "URI": "java.net",
    "List": "java.util",
    "Map": "java.util",
    "Set": "java.util",
    "ArrayList": "java.util",
    "HashMap": "java.util",
    "HashSet": "java.util",
    "LinkedHashMap": "java.util",
    "Optional": "java.util",
    "Objects": "java.util",
    "Arrays": "java.util",
    "Collections": "java.util",
    "UUID": "java.util",
    "Collectors": "java.util.stream",
    "IntStream": "java.util.stream",
    "Duration": "java.time",
    "Instant": "java.time",
    "TimeUnit": "java.util.concurrent",
    "CountDownLatch": "java.util.concurrent",
    "ExecutorService": "java.util.concurrent",
    "Executors": "java.util.concurrent",
    "Future": "java.util.concurrent",
    "CompletableFuture": "java.util.concurrent",
    "AtomicInteger": "java.util.concurrent.atomic",
    "AtomicLong": "java.util.concurrent.atomic",
}
_STRING_OR_COMMENT = re.compile(
    r'//[^\n]*|/\*.*?\*/|"""(?:.|\n)*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S
)
_IMPORT = re.compile(r"^import\s+(static\s+)?([\w.]+)(\.\*)?\s*;", re.M)
_PACKAGE = re.compile(r"^package\s+[\w.]+\s*;[ \t]*\n", re.M)
_USE = {n: re.compile(rf"(?<![\w.$]){n}\b") for n in JDK_TYPES}


def ensure_java_imports(files: list[GeneratedFile]) -> tuple[list[GeneratedFile], list[str]]:
    """테스트 파일(*.java)이 쓰는 JDK 이름 중 import 하지 않은 것을 package 줄 뒤에 더한다."""
    out: list[GeneratedFile] = []
    notes: list[str] = []
    for f in files:
        if not f.path.endswith(".java"):
            out.append(f)
            continue
        code = _STRING_OR_COMMENT.sub(" ", f.content)
        simple: set[str] = set()  # 이미 import 한 단순 이름 (다른 패키지의 같은 이름 포함)
        wildcard: set[str] = set()
        for static, name, star in _IMPORT.findall(code):
            if static:
                continue
            if star:
                wildcard.add(name)
            else:
                simple.add(name.rsplit(".", 1)[-1])
        own = set(re.findall(r"\b(?:class|interface|record|enum)\s+(\w+)", code))
        missing = [
            n
            for n, pkg in JDK_TYPES.items()
            if n not in simple and n not in own and pkg not in wildcard and _USE[n].search(code)
        ]
        if not missing:
            out.append(f)
            continue
        lines = "".join(f"import {JDK_TYPES[n]}.{n};\n" for n in missing)
        m = _PACKAGE.search(f.content)
        content = (
            f.content[: m.end()] + "\n" + lines + f.content[m.end() :]
            if m is not None
            else lines + f.content
        )
        out.append(GeneratedFile(path=f.path, content=content))
        notes.append(f"{f.path}: JDK import {', '.join(missing)} 를 더함")
    return out, notes
