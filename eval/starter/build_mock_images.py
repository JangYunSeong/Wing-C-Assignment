"""성공한 빌드(status=review)로 과제마다 응시용·채점용 이미지를 만들어 목 데이터로 묶는다 (docs/design/starter-templates.md §6.1).

    uv run python -m eval.starter.build_mock_images            # 기본 3과제
    uv run python -m eval.starter.build_mock_images --run data/eval/build/<빌드 폴더> ...

층: 런타임(Java: JDK 17 / Node: node 22 bookworm-slim / Python: python 3.11 slim-bookworm) → 템플릿 층(템플릿 project/ + 오프라인
의존성 — Gradle 캐시·node_modules·uv.lock 그대로 만든 가상 환경 /opt/venv, 하네스·manifest 없음) → 과제 층. 빌드 폴더의 직군(report.json sub)으로
템플릿을 고른다 (select_template — 2026-11-09 React·데이터 템플릿).
  응시용 = 템플릿 층 + 시작 코드 + README (숨은 테스트 없음)
  채점용 = 템플릿 층 + python3 + grade(Java: eval/starter/image_grade.py · Node: image_grade_node.py · Python: image_grade_py.py)
           + 숨은 테스트·하네스·조절값 + config.json
참조 구현(ref_*)은 어느 이미지에도 넣지 않는다 — 검증 때 제출물로만 쓴다.

검증 (모두 --network none): 응시용에서 기존 테스트가 오프라인으로 통과, 채점용으로 시작 코드만·허용 구현·흔한 구현 제출물을 채점.
응시용 이미지의 모든 레이어에 금지 경로(숨은 테스트·하네스·manifest)가 없는지 저장한 묶음에서 확인한다.
출력: data/mock_images/<날짜>/ — images.tar · manifest.json · samples/ (git 밖, 전달용). 빌드 작업 폴더는 data/mock_images/build-context/<날짜>/. podman 저장소는 data/mock_images/storage 에
넣되 이 작업 이름(localhost/task…)으로만 — 다른 이미지·컨테이너는 건드리지 않는다. 빌드 임시 파일은 data/mock_images/tmp. 지원자 데이터 없음 — 공고·재료·명세에서 만든 공통 과제.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from eval.starter import python_verify
from eval.starter.gradle_verify import case_ids, write_overlay
from eval.starter.node_verify import layout_of
from task_spec.schemas import GeneratedFile, HiddenPlan, TaskContract, TaskItem
from task_spec.starter.render import render_readme
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import (
    Template,
    candidate_layer_rules,
    forbidden_in,
    is_java,
    is_python,
    load_template,
    select_template,
)

ROOT = Path(__file__).resolve().parents[2]
BUILDS = ROOT / "data" / "eval" / "build"
DEFAULT_RUNS = [  # 과제별 최신 성공 빌드 (status=review — 2026-11-08 사용자 선택 "과제별 최신 1개씩")
    "2026-11-08-alpha-full-sg004",  # 테스트 먼저 흐름 (공개 테스트 있음)
    "2026-11-08-alpha-full-ae023",  # 테스트 먼저 흐름 (공개 테스트 있음)
    "2026-11-07-alpha-kakaobank005-r2",  # 같은 날 전체 알파는 하네스 버그로 failed — 성공 판정 받은 빌드만
    "2026-11-07-loop-delivery-r5",
    # 프론트엔드 React (2026-11-09 알파 3공고 — 전부 review)
    "2026-11-09-alpha-fe-kb007-r4",
    "2026-11-09-alpha-fe-naver003",
    "2026-11-09-alpha-fe-autoever048-r2",
]
JAVA_TEMPLATE = "backend/java-spring-boot3-jpa"
PYTHON3 = "RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*"


@dataclass(frozen=True)
class Stack:
    """스택마다 다른 것 — 런타임 이미지·의존성 설치·grade 스크립트·응시용 오프라인 확인 명령. 층 구조·검증 순서는 같다."""

    base: str
    install: tuple[str, ...]  # 템플릿 층 — WORKDIR(작업 경로)에 project/ 를 복사한 뒤
    grade_script: Path
    candidate_check: tuple[
        str, ...
    ]  # 응시용 이미지에서 네트워크 없이 — 의존성이 이미지 안에 다 있는가
    scan_skip: tuple[str, ...]  # 레이어 검사에서 뺄 런타임 경로 (작업 경로가 아닌 곳)
    grader_setup: tuple[str, ...] = (
        PYTHON3,
    )  # 채점 기반 층 — grade 스크립트를 돌릴 python3 (Python 런타임은 이미 있음)


JAVA = Stack(
    base="docker.io/library/eclipse-temurin:17-jdk-jammy",
    install=(
        "ENV GRADLE_USER_HOME=/opt/gradle-home",
        # 뼈대를 한 번 빌드·테스트해 Gradle 배포판·의존성(테스트 런타임 포함)을 캐시에 — 채점·응시 컨테이너는 네트워크 없음
        "RUN chmod +x gradlew && ./gradlew --no-daemon --console=plain build && rm -rf build .gradle",
    ),
    grade_script=ROOT / "eval" / "starter" / "image_grade.py",
    candidate_check=("./gradlew", "--offline", "--no-daemon", "-q", "test"),
    scan_skip=("usr/", "opt/java/", "etc/", "var/", "lib/"),
)
# Debian 12(bookworm) — Playwright 공식 지원 배포판이라 실제 브라우저 층도 같은 계열로 (alpine/musl 은 미지원, 2026-11-09 문서 확인)
NODE = Stack(
    base="docker.io/library/node:22-bookworm-slim",
    install=(
        # lock 그대로 설치하고 뼈대의 타입 검사·테스트를 한 번 — 응시·채점 컨테이너는 네트워크 없음
        "RUN npm ci --no-audit --no-fund && npx tsc --noEmit && npx vitest run && rm -rf node_modules/.vite node_modules/.vitest",
    ),
    grade_script=ROOT / "eval" / "starter" / "image_grade_node.py",
    candidate_check=("sh", "-c", "npx tsc --noEmit && npm test"),
    scan_skip=("usr/", "opt/", "etc/", "var/", "lib/"),
)


# Python 3.11(템플릿 requires-python) Debian 12 slim — uv.lock 그대로 /opt/venv 에 설치, PATH 앞에. 응시용에서 `python -m pytest` 가 그 환경
PY_VENV = "/opt/venv"
UV_VERSION = "0.13.6"  # 개발 서버와 같은 uv (lock 형식이 같게)
PY = Stack(
    base="docker.io/library/python:3.11-slim-bookworm",
    install=(
        f"ENV UV_PROJECT_ENVIRONMENT={PY_VENV} UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy PATH={PY_VENV}/bin:$PATH",
        # lock 그대로 설치하고 뼈대 테스트를 한 번 — 응시·채점 컨테이너는 네트워크 없음. 캐시·바이트코드는 지운다
        f"RUN pip install --no-cache-dir uv=={UV_VERSION} && uv sync --frozen --no-install-project && uv cache clean "
        "&& python -m pytest -q -p no:cacheprovider && find . -name __pycache__ -prune -exec rm -rf {} +",
    ),
    grade_script=ROOT / "eval" / "starter" / "image_grade_py.py",
    candidate_check=("python", "-m", "pytest", "-q", "-p", "no:cacheprovider"),
    scan_skip=("usr/", "opt/", "etc/", "var/", "lib/", "root/"),
    grader_setup=(),
)


def stack_for(t: Template) -> Stack:
    if is_java(t.manifest):
        return JAVA
    return PY if is_python(t.manifest) else NODE


def template_of(run: Path) -> Template:
    """빌드 폴더의 직군 → 템플릿 (직군이 없는 손 예시 루프는 Java)."""
    report = json.loads((run / "report.json").read_text("utf-8"))
    sub = report.get("sub")
    return (select_template(sub) if sub else None) or load_template(JAVA_TEMPLATE)


PUBLIC_ID = r"\bpt\d{1,2}\b"  # 공개 테스트 클래스의 @DisplayName 에 적힌 공개 테스트 id
TIMEOUT_S = 1200
TEST_COMMAND = ["./gradlew", "--offline", "--no-daemon", "--console=plain", "--continue", "test"]


class Podman:
    """전용 저장소로만 podman 을 부른다 (기본 저장소의 이미지·컨테이너는 건드리지 않음)."""

    def __init__(self, tmp: Path, store: Path | None = None) -> None:
        # store 를 주면 그 경로를 podman 저장소로 (SELinux 컨테이너 레이블이 있는 경로여야 컨테이너가 돈다 — 2026-11-08: /ssd 아래
        # 전용 저장소는 unlabeled_t 라 RUN 이 libc 로드에서 막힘). 기본은 시스템 저장소에 이 작업 이름(localhost/task…)으로만 추가.
        self.flags = (
            ["--root", str(store / "root"), "--runroot", str(store / "run")] if store else []
        )
        self.env = os.environ | {"TMPDIR": str(tmp)}  # 빌드 임시 파일은 여유가 큰 쪽에
        tmp.mkdir(parents=True, exist_ok=True)

    def __call__(
        self, *args: str, log: Path | None = None, timeout: int = 3600, check: bool = True
    ) -> str:
        cmd = ["podman", *self.flags, *args]
        if log is None:
            res = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=timeout)
            if check and res.returncode != 0:
                raise RuntimeError(f"{' '.join(args[:3])} 실패: {res.stderr[-2000:]}")
            return res.stdout
        with log.open("a") as fh:
            fh.write(f"\n$ podman {' '.join(args)}\n")
            fh.flush()
            res = subprocess.run(
                cmd, env=self.env, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout
            )
        if check and res.returncode != 0:
            raise RuntimeError(f"{' '.join(args[:3])} 실패 (로그 {log})")
        return ""


def _overlay(kind: str, root: Path, label: str = "") -> Overlay | None:
    if not root.is_dir():
        return None
    files = tuple(
        GeneratedFile(path=str(p.relative_to(root)), content=p.read_text("utf-8"))
        for p in sorted(root.rglob("*"))
        if p.is_file()
    )
    return Overlay(kind=kind, files=files, label=label)  # type: ignore[arg-type]


def _common_id(task_id: str) -> str:
    raw = re.sub(r"^(alpha|smoke)-", "", task_id).replace("#", "-").lower()
    return re.sub(r"[^a-z0-9_.-]+", "-", raw).strip("-")


def load_run(run: Path, t: Template) -> dict[str, Any]:
    """빌드 폴더 → 과제 재료 (명세·계약·숨은 계획·오버레이·README)."""
    report = json.loads((run / "report.json").read_text("utf-8"))
    status = (report.get("build") or report).get("status")
    if status != "review":
        raise ValueError(f"{run.name}: 성공한 빌드가 아님 (status={status})")
    if (run / "task.json").is_file():
        task = TaskItem.model_validate_json((run / "task.json").read_text("utf-8"))
        contract_file = run / "contract.json"
    else:  # 손 예시 루프 — 명세는 손 예시에서, 계약은 그때 쓴 파일에서
        from eval.starter.run_plan_contract import build_task

        task, _ = build_task(ROOT / "data" / "feature_starters" / "delivery-audit")
        contract_file = ROOT / str(report["contract"])
    data = json.loads(contract_file.read_text("utf-8"))
    contract = TaskContract.model_validate(data["contract"])
    plan = HiddenPlan.model_validate(data["hidden_plan"])
    ov = run / "overlays"
    readme = (
        (run / "candidate" / "README.md").read_text("utf-8")
        if (run / "candidate" / "README.md").is_file()
        else render_readme(task, contract, t).content
    )
    return {
        "run": run.name,
        "task": task,
        "contract": contract,
        "plan": plan,
        "common_id": _common_id(task.task_id),
        "revision": int(
            report.get("revision") or 1
        ),  # 같은 공통 과제의 다른 빌드는 판을 나눠 이미지 이름이 겹치지 않게
        "ontology_version": report.get("ontology_version") or "unknown",
        "readme": readme,
        "starter": _overlay("starter", ov / "starter"),
        "hidden": _overlay("hidden", ov / "hidden"),
        "public": _overlay("public_test", ov / "public"),
        "refs": {
            name: _overlay(
                "ref_naive" if name.startswith("naive") else "ref_accepted", ov / name, name
            )
            for name in sorted(p.name for p in ov.iterdir() if p.is_dir())
            if name not in ("starter", "hidden", "public")
        },
    }


def tree_sha256(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        h.update(
            str(p.relative_to(root)).encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest()
        )
    return h.hexdigest()


def candidate_tree(t: Template, item: dict[str, Any], dest: Path) -> None:
    """템플릿 project/ + 시작 코드 + README — 응시자가 받는 작업 경로 그대로."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(t.project, dest)
    write_overlay(item["starter"], dest)
    (dest / "README.md").write_text(item["readme"], "utf-8")
    if (dest / "gradlew").is_file():
        (dest / "gradlew").chmod(0o755)


def grader_overlay(t: Template, item: dict[str, Any], dest: Path) -> dict[str, Any]:
    """채점 때 제출물 위에 얹는 것 — 숨은 테스트·하네스·자체 점검·조절값(+ 공개 테스트) — 와 grade 설정."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    hl = t.manifest.hidden_layout
    assert hl is not None
    for sub, target in (("hidden/harness", hl.harness), ("hidden/selftest", hl.selftest)):
        for rel in t.files(sub):
            (dest / target / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(t.root / sub / rel, dest / target / rel)
    write_overlay(item["hidden"], dest)
    if item["public"] is not None:
        write_overlay(item["public"], dest)
    m = t.manifest
    if is_python(m):
        return py_config(t, item)
    if not is_java(m):
        return node_config(t, item)
    classes = case_ids(item["hidden"], m.hidden_rules.case_id_pattern)
    cases = [
        {
            "id": c.case_id,
            "trap_id": c.trap_id,
            "intended_reason": c.intended_reason,
            "kind": "hidden",
        }
        for c in item["plan"].cases
    ]
    public = item["public"]
    if public is not None:  # 공개 테스트 — pt id 가 있으면 그 id, 없으면(저장 확인 등) 클래스 이름
        pub_ids = case_ids(public, PUBLIC_ID)
        classes |= pub_ids
        helper = m.public_helper.path if m.public_helper else None
        for f in public.files:
            stem = Path(f.path).stem
            if f.path.endswith("Test.java") and f.path != helper:
                cases.append({"id": pub_ids.get(stem, stem), "kind": "public"})
    return {
        "common_id": item["common_id"],
        "revision": item["revision"],
        "template_ref": m.ref,
        "template_dir": m.workspace,
        "locked": list(m.locked),
        "reserved": list(m.reserved),
        "test_command": TEST_COMMAND,
        "timeout_s": TIMEOUT_S,
        "classes": classes,
        "cases": cases,
    }


def node_config(t: Template, item: dict[str, Any]) -> dict[str, Any]:
    """Node grade 설정 (image_grade_node) — 케이스 id 는 describe 제목(숨은: hc*, 공개: pt*)으로 판정이 찾는다."""
    m = t.manifest
    cmds = m.commands
    assert cmds is not None and cmds.test_compile and cmds.hidden_test
    cases: list[dict[str, Any]] = [
        {
            "id": c.case_id,
            "trap_id": c.trap_id,
            "intended_reason": c.intended_reason,
            "kind": "hidden",
        }
        for c in item["plan"].cases
    ]
    if item["public"] is not None:
        cases += [{"id": pt.test_id, "kind": "public"} for pt in item["contract"].public_tests]
    return {
        "common_id": item["common_id"],
        "revision": item["revision"],
        "template_ref": m.ref,
        "template_dir": m.workspace,
        "locked": list(m.locked),
        "reserved": list(m.reserved),
        "compile_command": shlex.split(cmds.test_compile),
        "existing_command": ["npx", "vitest", "run"],
        "grading_command": shlex.split(cmds.hidden_test),
        "layout": layout_of(t),
        "case_id_pattern": m.hidden_rules.case_id_pattern,
        "timeout_s": TIMEOUT_S,
        "cases": cases,
    }


def py_config(t: Template, item: dict[str, Any]) -> dict[str, Any]:
    """Python grade 설정 (image_grade_py) — 개발 서버 Python 검증기와 같은 명령·대상 (케이스 id 는 테스트 이름의 hc*·pt*)."""
    m = t.manifest
    cases: list[dict[str, Any]] = [
        {
            "id": c.case_id,
            "trap_id": c.trap_id,
            "intended_reason": c.intended_reason,
            "kind": "hidden",
        }
        for c in item["plan"].cases
    ]
    if item["public"] is not None:
        cases += [{"id": pt.test_id, "kind": "public"} for pt in item["contract"].public_tests]
    return {
        "common_id": item["common_id"],
        "revision": item["revision"],
        "template_ref": m.ref,
        "template_dir": m.workspace,
        "locked": list(m.locked),
        "reserved": list(m.reserved),
        "python": f"{PY_VENV}/bin/python",
        "compile_paths": list(python_verify.COMPILE_PATHS),
        "grading_paths": python_verify.grading_paths(t),
        "grading_ini": python_verify.GRADING_INI,
        "grading_rootdir": python_verify.GRADING_ROOTDIR,
        "layout": python_verify.layout_of(t),
        "case_id_pattern": m.hidden_rules.case_id_pattern,
        "timeout_s": TIMEOUT_S,
        "cases": cases,
    }


def containerfile(base: str, body: list[str]) -> str:
    return "\n".join([f"FROM {base}", *body, ""])


def labels(d: dict[str, Any]) -> list[str]:
    return [f"LABEL {k}={json.dumps(str(v), ensure_ascii=False)}" for k, v in d.items()]


def scan_candidates(archive: Path, targets: dict[str, Template]) -> dict[str, list[str]]:
    """저장한 묶음에서 응시용 이미지의 레이어마다 파일 이름을 꺼내 금지 경로를 찾는다 (위 레이어에서 지워도 아래에 남으므로 레이어 단위).
    targets: 응시용 이미지 이름 → 그 과제의 템플릿 (금지 경로는 템플릿 manifest 에서)."""
    names = list(targets)
    out: dict[str, list[str]] = {}
    with tarfile.open(archive) as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())  # type: ignore[union-attr]
        for entry in manifest:
            tags = entry.get("RepoTags") or []
            name = next((n for n in names if any(tag.startswith(n) for tag in tags)), None)
            if name is None:
                continue
            rules, skip = candidate_layer_rules(targets[name]), stack_for(targets[name]).scan_skip
            bad: list[str] = []
            for layer in entry["Layers"]:
                with tarfile.open(fileobj=tar.extractfile(layer)) as lt:  # type: ignore[arg-type]
                    paths = [m.name for m in lt.getmembers() if m.isfile()]
                bad += forbidden_in([p for p in paths if not p.startswith(skip)], rules)
            out[name] = sorted(set(bad))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--run", action="append", default=[], help="빌드 폴더 (data/eval/build/<이름>) — 여러 번"
    )
    ap.add_argument("--out", default=str(ROOT / "data" / "mock_images" / str(date.today())))
    ap.add_argument("--store", default=None, help="podman 저장소 경로 (기본: 시스템 저장소)")
    ap.add_argument("--skip-verify", action="store_true")
    a = ap.parse_args()
    runs = [Path(r) if "/" in r else BUILDS / r for r in (a.run or DEFAULT_RUNS)]
    run_templates = [(run, template_of(run)) for run in runs]
    out = Path(
        a.out
    ).resolve()  # podman -v 는 상대 경로를 이름 있는 볼륨으로 본다 (2026-11-09: 채점 결과가 비었다)
    # 빌드 작업 폴더(참조 구현·숨은 테스트 평문 포함)와 로그는 전달 폴더 밖에 — 전달물에 섞이지 않게
    ctx = ROOT / "data" / "mock_images" / "build-context" / out.name
    out.mkdir(parents=True, exist_ok=True)
    ctx.mkdir(parents=True, exist_ok=True)
    log = ctx / "build.log"
    pm = Podman(ROOT / "data" / "mock_images" / "tmp", Path(a.store) if a.store else None)

    t0 = time.monotonic()
    layers: dict[str, tuple[str, str]] = {}  # 템플릿 ref → (템플릿 층 이미지, 채점 기반 이미지)
    for t in {t.manifest.ref: t for _, t in run_templates}.values():
        st = stack_for(t)
        stem = t.manifest.ref.replace("@", "-").replace(
            "/", "-"
        )  # backend-java-spring-boot3-jpa-v1
        template_img = f"localhost/task-template/{stem}"
        grader_base = f"localhost/task-grader-base/{stem}"
        print(f"① 템플릿 층 {t.manifest.ref} (project/ + 오프라인 의존성, {st.base})", flush=True)
        tdir = ctx / "template" / stem
        if tdir.exists():
            shutil.rmtree(tdir)
        shutil.copytree(t.project, tdir / "project")
        (tdir / "Containerfile").write_text(
            containerfile(
                st.base,
                [
                    f"WORKDIR {t.manifest.workspace}",
                    f"COPY project/ {t.manifest.workspace}/",
                    *st.install,
                    *labels({"template_ref": t.manifest.ref}),
                ],
            ),
            "utf-8",
        )
        pm("build", "-t", template_img, str(tdir), log=log)
        print(
            f"② 채점 기반 층 {t.manifest.ref} (python3 + grade)", flush=True
        )  # Python 런타임은 python3 설치 생략
        gdir = ctx / "grader-base" / stem
        gdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(st.grade_script, gdir / "grade.py")
        (gdir / "Containerfile").write_text(
            containerfile(
                template_img,
                [
                    *st.grader_setup,
                    "COPY grade.py /grader/grade.py",
                    "RUN printf '#!/bin/sh\\nexec python3 /grader/grade.py \"$@\"\\n' > /usr/local/bin/grade && chmod +x /usr/local/bin/grade",
                ],
            ),
            "utf-8",
        )
        pm("build", "-t", grader_base, str(gdir), log=log)
        layers[t.manifest.ref] = (template_img, grader_base)

    tasks: list[dict[str, Any]] = []
    for run, t in run_templates:
        template_img, grader_base = layers[t.manifest.ref]
        item = load_run(run, t)
        cid, rev = item["common_id"], item["revision"]
        print(f"③ {cid} ({run.name})", flush=True)
        cdir, kdir = ctx / cid / "candidate", ctx / cid / "grader"
        candidate_tree(t, item, cdir / "workspace")
        paths = [
            str(p.relative_to(cdir / "workspace"))
            for p in (cdir / "workspace").rglob("*")
            if p.is_file()
        ]
        leaked = forbidden_in(paths, candidate_layer_rules(t))
        if leaked:
            raise RuntimeError(f"{cid}: 응시용 작업 경로에 금지 경로 {leaked}")
        tree = tree_sha256(cdir / "workspace")
        common = {
            "common_id": cid,
            "revision": rev,
            "template_ref": t.manifest.ref,
            "ontology_version": item["ontology_version"],
        }
        cand = f"localhost/task/{cid}-r{rev}-candidate"
        (cdir / "Containerfile").write_text(
            containerfile(
                template_img,
                [
                    f"COPY workspace/ {t.manifest.workspace}/",
                    *labels(
                        {
                            **common,
                            "kind": "candidate",
                            "tree_sha256": tree,
                            "workspace": t.manifest.workspace,
                        }
                    ),
                    'CMD ["bash"]',
                ],
            ),
            "utf-8",
        )
        pm("build", "-t", cand, str(cdir), log=log)
        config = grader_overlay(t, item, kdir / "overlay")
        (kdir / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=1), "utf-8")
        grader = f"localhost/task/{cid}-r{rev}-grader"
        (kdir / "Containerfile").write_text(
            containerfile(
                grader_base,
                [
                    "COPY overlay/ /grader/overlay/",
                    "COPY config.json /grader/config.json",
                    *labels(
                        {
                            **common,
                            "kind": "grader",
                            "tree_sha256": tree,
                            "grade.timeout_s": TIMEOUT_S,
                        }
                    ),
                    'CMD ["grade"]',
                ],
            ),
            "utf-8",
        )
        pm("build", "-t", grader, str(kdir), log=log)
        tasks.append(
            {
                "item": item,
                "candidate": cand,
                "grader": grader,
                "tree": tree,
                "cdir": cdir,
                "config": config,
                "template": t,
            }
        )

    samples = out / "samples"
    if not a.skip_verify:
        for x in tasks:
            cid, item = x["item"]["common_id"], x["item"]
            print(f"④ 검증 {cid}", flush=True)
            sdir = samples / cid
            sdir.mkdir(parents=True, exist_ok=True)
            # 응시용: 네트워크 없이 기존 테스트가 통과하는가 (의존성이 이미지 안에 다 있는가)
            res = subprocess.run(
                [
                    "podman",
                    *pm.flags,
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    x["candidate"],
                    *stack_for(x["template"]).candidate_check,
                ],
                env=pm.env,
                capture_output=True,
                text=True,
                timeout=1800,
            )
            (sdir / "candidate-offline-test.log").write_text(
                res.stdout[-20000:] + res.stderr[-20000:], "utf-8"
            )
            x["candidate_offline_test"] = (
                "pass" if res.returncode == 0 else f"fail ({res.returncode})"
            )
            subs = {
                "starter-only": None,
                **{k: v for k, v in item["refs"].items() if v is not None},
            }
            x["grades"] = {}
            for name, ref in subs.items():
                sub = ctx / cid / "submissions" / name
                if sub.exists():
                    shutil.rmtree(sub)
                shutil.copytree(x["cdir"] / "workspace", sub)
                if ref is not None:
                    write_overlay(ref, sub)
                res_dir = sdir / f"grade-{name}"
                if res_dir.exists():
                    shutil.rmtree(res_dir)
                res_dir.mkdir(parents=True)
                g0 = time.monotonic()
                proc = subprocess.run(
                    [
                        "podman",
                        *pm.flags,
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "-v",
                        f"{sub}:/submission:ro,z",
                        "-v",
                        f"{res_dir}:/out:z",
                        x["grader"],
                        "grade",
                    ],
                    env=pm.env,
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_S + 300,
                )
                result = (
                    json.loads((res_dir / "result.json").read_text("utf-8"))
                    if (res_dir / "result.json").is_file()
                    else None
                )
                if result is None:  # 채점 컨테이너가 결과를 못 냄 — 출력을 남긴다
                    (sdir / f"grade-{name}.err").write_text(
                        f"exit {proc.returncode}\n{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}",
                        "utf-8",
                    )
                x["grades"][name] = {"seconds": round(time.monotonic() - g0, 1), "result": result}
                print(
                    f"   {name}: {json.dumps((result or {}).get('summary'), ensure_ascii=False)} setup={(result or {}).get('setup')}",
                    flush=True,
                )

    names = [n for x in tasks for n in (x["candidate"], x["grader"])]
    print("⑤ 저장 (여러 이미지 한 묶음 — 공통 레이어는 한 번)", flush=True)
    archive = out / "images.tar"
    if archive.exists():
        archive.unlink()
    pm(
        "save",
        "--multi-image-archive",
        "--format",
        "docker-archive",
        "-o",
        str(archive),
        *names,
        log=log,
    )
    leaks = scan_candidates(archive, {x["candidate"]: x["template"] for x in tasks})
    print(f"   응시용 레이어 금지 경로: {leaks}", flush=True)

    entries = []
    for x in tasks:
        item, t = x["item"], x["template"]
        info = {}
        for role in ("candidate", "grader"):
            insp = json.loads(pm("image", "inspect", x[role]))[0]
            info[role] = {
                "name": x[role],
                "image_id": "sha256:" + insp["Id"].removeprefix("sha256:"),
                "size_mb": round(insp["Size"] / 1e6, 1),
                "labels": insp.get("Labels") or {},
            }
        entries.append(
            {
                "common_id": item["common_id"],
                "revision": item["revision"],
                "title": item["task"].title,
                "source_build": item["run"],
                "template_ref": t.manifest.ref,
                "base_image": stack_for(t).base,
                "candidate": {
                    **info["candidate"],
                    "workspace": t.manifest.workspace,
                    "run": f"podman run -it --name <지원자> {x['candidate']} bash",
                    "commands": t.manifest.commands.model_dump() if t.manifest.commands else {},
                    "offline_existing_tests": x.get("candidate_offline_test"),
                    "forbidden_paths_in_layers": leaks.get(x["candidate"], []),
                },
                "grader": {
                    **info["grader"],
                    "grade": f"podman run --rm --network none -v <제출물>:/submission:ro -v <결과>:/out {x['grader']} grade",
                    "timeout_s": TIMEOUT_S,
                    "cases": x["config"]["cases"],
                },
                "verification": {
                    name: {
                        "seconds": g["seconds"],
                        "setup": (g["result"] or {}).get("setup"),
                        "summary": (g["result"] or {}).get("summary"),
                    }
                    for name, g in x.get("grades", {}).items()
                },
            }
        )
    (out / "manifest.json").write_text(
        json.dumps(
            {
                "created": str(date.today()),
                "archive": "images.tar",
                "archive_format": "docker-archive (podman save --multi-image-archive) — podman load -i images.tar",
                "base_images": {t.manifest.ref: stack_for(t).base for _, t in run_templates},
                "tasks": entries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    print(
        f"→ {out} ({round(time.monotonic() - t0)}초, images.tar {round(archive.stat().st_size / 1e9, 2)} GB)",
        flush=True,
    )


if __name__ == "__main__":
    main()
