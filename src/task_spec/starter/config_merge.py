"""생성 코드의 설정 파일을 바탕 설정 위에 키 단위로 합친다 (템플릿 manifest `config_merge`).

생성기는 설정 파일을 통째로 다시 쓰기 쉽다 — 그러면 템플릿이 정한 키(스키마 관리 방식·DB 주소 등)가 말없이 사라진다.
검사로 잡는 대신 어긋날 수 없게 만든다: 바탕(템플릿 → 시작 코드) 위에 생성 코드의 키를 얹고, 잠긴 키는 늘 바탕 값으로 되돌린다.
바꾼 것은 adjustments 로 남겨 승인 화면에 보인다. 주석은 남지 않는다 (YAML 을 다시 쓰므로).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml

from task_spec.schemas.build import GeneratedFile
from task_spec.schemas.template import TemplateManifest

_MISSING = object()


def _nest(doc: Mapping[str, Any]) -> dict[str, Any]:
    """점 표기 키(`spring.jpa.open-in-view: false`)를 중첩으로 펼친다 — 같은 설정을 두 꼴로 써도 한 키로 합쳐지게."""
    out: dict[str, Any] = {}
    for key, value in doc.items():
        parts = str(key).split(".")
        node = out
        for p in parts[:-1]:
            child = node.get(p)
            if not isinstance(child, dict):
                child = node[p] = {}
            node = child
        value = _nest(value) if isinstance(value, Mapping) else value
        if isinstance(value, dict) and isinstance(node.get(parts[-1]), dict):
            node[parts[-1]] = _deep(node[parts[-1]], value)
        else:
            node[parts[-1]] = value
    return out


def _deep(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in top.items():
        out[k] = _deep(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _get(doc: dict[str, Any], key: str) -> Any:
    node: Any = doc
    for p in key.split("."):
        if not isinstance(node, dict) or p not in node:
            return _MISSING
        node = node[p]
    return node


def _set(doc: dict[str, Any], key: str, value: Any) -> None:
    *parents, last = key.split(".")
    node = doc
    for p in parents:
        child = node.get(p)
        if not isinstance(child, dict):
            if value is _MISSING:
                return
            child = node[p] = {}
        node = child
    if value is _MISSING:
        node.pop(last, None)
    else:
        node[last] = value


def _prune(doc: dict[str, Any]) -> dict[str, Any]:
    """잠긴 키를 지워 빈 묶음만 남은 곳을 걷어낸다."""
    out: dict[str, Any] = {}
    for k, v in doc.items():
        if isinstance(v, dict):
            v = _prune(v)
            if not v:
                continue
        out[k] = v
    return out


def _load(text: str) -> dict[str, Any] | None:
    """문서 하나짜리 매핑만 — 여러 문서(`---` 프로필)·읽지 못하는 YAML 은 None (합치지 않고 그대로 둔다)."""
    try:
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError:
        return None
    if not docs:
        return {}
    if len(docs) > 1 or not isinstance(docs[0], dict):
        return None
    return _nest(docs[0])


def merge_config(
    files: list[GeneratedFile], manifest: TemplateManifest, base: Mapping[str, str]
) -> tuple[list[GeneratedFile], list[str]]:
    """config_merge 경로의 생성 파일을 base(경로 → 바탕 설정 원문) 위에 합친다. (바꾼 파일, 바꾼 내용 설명)."""
    rule = manifest.config_merge
    if rule is None:
        return files, []
    out: list[GeneratedFile] = []
    notes: list[str] = []
    for f in files:
        under = _load(base.get(f.path, "")) if f.path in rule.paths else None
        mine = _load(f.content) if under is not None else None
        if under is None or mine is None:
            out.append(f)
            continue
        merged = _deep(under, mine)
        relocked = []
        for key in rule.locked_keys:
            want, got = _get(under, key), _get(mine, key)
            if got is not _MISSING and got != want:
                relocked.append(key)
            _set(merged, key, want)
        merged = _prune(merged)
        if merged == mine:
            out.append(f)
            continue
        kept = sorted(k for k in _keys(under) if _get(mine, k) is _MISSING)
        text = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True, default_flow_style=False)
        out.append(GeneratedFile(path=f.path, content=text))
        detail = []
        if kept:
            detail.append(f"생성 코드가 빠뜨린 바탕 설정 {', '.join(kept)} 을 되살림")
        if relocked:
            detail.append(f"잠긴 설정 {', '.join(relocked)} 을 바탕 값으로 되돌림")
        notes.append(
            f"{f.path} 를 바탕 설정 위에 합침"
            + (f" — {'; '.join(detail)}" if detail else "")
            + " (설정 파일은 덮어쓰지 않고 키 단위로 합친다)"
        )
    return out, notes


def _keys(doc: dict[str, Any], prefix: str = "") -> list[str]:
    out: list[str] = []
    for k, v in doc.items():
        key = f"{prefix}{k}"
        out += _keys(v, key + ".") if isinstance(v, dict) and v else [key]
    return out
