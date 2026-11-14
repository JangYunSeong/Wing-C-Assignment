"""성공한 빌드(status=review)의 재료로 응시용·채점용 이미지를 만들고 검증한다 (docs/design/starter-templates.md §6.1).

층: 런타임(Java: JDK 17 / Node: node 22 bookworm-slim / Python: python 3.11 slim-bookworm) → 템플릿 층(템플릿 project/ + 오프라인
의존성 — Gradle 캐시·node_modules·uv.lock 그대로 만든 가상 환경 /opt/venv, 하네스·manifest 없음) → 과제 층.
  응시용 = 템플릿 층 + 시작 코드 + README (숨은 테스트 없음)
  채점용 = 템플릿 층 + python3 + grade(`starter/grade/` — Java `image_grade` · Node `image_grade_node` · Python `image_grade_py`)
           + 숨은 테스트·하네스·조절값 + config.json
참조 구현(ref_*)은 어느 이미지에도 넣지 않는다 — 검증 때 제출물로만 쓴다.

검증 (모두 --network none): 응시용에서 기존 테스트가 오프라인으로 통과, 채점용으로 시작 코드만·허용 구현·흔한 구현 제출물을 채점.
응시용 이미지의 모든 레이어에 금지 경로(숨은 테스트·하네스·manifest)가 없는지 저장한 묶음에서 확인한다 (`scan_candidates`).
런타임 층(스택·Podman·Containerfile)은 `starter/layers.py` — 검증기도 같은 층 위에서 돈다 (#127).

2026-11-12 eval/starter/build_mock_images.py 에서 옮김 (#126) — 빌드 폴더를 읽는 부분(`load_run`)과 명령줄은 eval 에 남았다.
이 모듈은 재료(`TaskMaterial`)만 받는다. 지원자 데이터 없음 — 공고·재료·명세에서 만든 공통 과제.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from task_spec.schemas import HiddenPlan, TaskContract, TaskItem
from task_spec.starter.grade import image_grade
from task_spec.starter.layers import (  # 런타임 층 정의 — 검증기(#127)와 같이 쓴다
    PY_VENV,
    Podman,
    containerfile,
    labels,
    stack_for,
)
from task_spec.starter.static_check import Overlay
from task_spec.starter.templates import (
    Template,
    candidate_layer_rules,
    forbidden_in,
    is_java,
    is_python,
)
from task_spec.starter.verify import python as python_verify
from task_spec.starter.verify.gradle import case_ids
from task_spec.starter.verify.node import layout_of
from task_spec.starter.verify.workspace import write_overlay

Progress = Callable[[str], None]


def _quiet(_: str) -> None:
    pass


PUBLIC_ID = r"\bpt\d{1,2}\b"  # 공개 테스트 클래스의 @DisplayName 에 적힌 공개 테스트 id
TIMEOUT_S = 1200
TEST_COMMAND = image_grade.gradle_command("test")  # 과제 생성 검증과 같은 정의 (#174)


@dataclass(frozen=True)
class TaskMaterial:
    """이미지 한 벌의 재료 — 명세·계약·숨은 계획·오버레이·README (빌드 결과에서)."""

    source: str  # 재료를 읽은 곳 (알파 빌드 폴더 이름) — manifest `source_build`
    task: TaskItem
    contract: TaskContract
    plan: HiddenPlan
    common_id: str
    revision: int  # 같은 공통 과제의 다른 빌드는 판을 나눠 이미지 이름이 겹치지 않게
    ontology_version: str
    readme: str
    starter: Overlay | None
    hidden: Overlay | None
    public: Overlay | None
    refs: dict[
        str, Overlay | None
    ]  # 참조 구현 변형 이름(d1·naive-all…) → 오버레이 — 이미지에는 넣지 않음

    @property
    def key(self) -> str:
        """작업 폴더·표본 이름 — 같은 공통 과제의 다른 판이 덮어쓰지 않게 (2026-11-10: r3 가 r1·r2 를 덮어 검증이 엇갈림)."""
        return f"{self.common_id}-r{self.revision}"


def _files_sha256(base: Path, files: list[Path]) -> str:
    """파일 목록 해시 — 파일마다 (base 기준 경로 + NUL + 내용 sha256), 주어진 순서대로."""
    h = hashlib.sha256()
    for p in files:
        h.update(
            str(p.relative_to(base)).encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest()
        )
    return h.hexdigest()


def _files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def tree_sha256(root: Path) -> str:
    """응시 작업 경로 해시 — 모든 파일 (schemas/package.py `tree_sha256`)."""
    return _files_sha256(root, _files_under(root))


def grader_sha256(kdir: Path) -> str:
    """채점용 내용 해시 — 숨은 overlay + config.json (Containerfile·라벨 제외, schemas/package.py `grader_sha256`)."""
    return _files_sha256(kdir, [*_files_under(kdir / "overlay"), kdir / "config.json"])


def candidate_tree(t: Template, item: TaskMaterial, dest: Path) -> None:
    """템플릿 project/ + 시작 코드 + README — 응시자가 받는 작업 경로 그대로."""
    if dest.exists():
        shutil.rmtree(dest)
    if item.starter is None:
        raise ValueError(f"{item.key}: 시작 코드가 없다")
    shutil.copytree(t.project, dest)
    write_overlay(item.starter, dest)
    (dest / "README.md").write_text(item.readme, "utf-8")
    if (dest / "gradlew").is_file():
        (dest / "gradlew").chmod(0o755)


def _hidden_cases(item: TaskMaterial) -> list[dict[str, Any]]:
    return [
        {
            "id": c.case_id,
            "trap_id": c.trap_id,
            "intended_reason": c.intended_reason,
            "kind": "hidden",
        }
        for c in item.plan.cases
    ]


def grader_overlay(t: Template, item: TaskMaterial, dest: Path) -> dict[str, Any]:
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
    if item.hidden is None:
        raise ValueError(f"{item.key}: 숨은 테스트가 없다")
    write_overlay(item.hidden, dest)
    if item.public is not None:
        write_overlay(item.public, dest)
    if is_python(t.manifest):
        return py_config(t, item)
    if is_java(t.manifest):
        return java_config(t, item)
    return node_config(t, item)


def java_config(t: Template, item: TaskMaterial) -> dict[str, Any]:
    """Java grade 설정 (image_grade) — 판정은 JUnit 테스트 클래스 이름 → 케이스 id (`classes`)로 결과를 찾는다."""
    m = t.manifest
    classes = case_ids(item.hidden, m.hidden_rules.case_id_pattern)
    cases = _hidden_cases(item)
    public = item.public
    if public is not None:  # 공개 테스트 — pt id 가 있으면 그 id, 없으면(저장 확인 등) 클래스 이름
        pub_ids = case_ids(public, PUBLIC_ID)
        classes |= pub_ids
        helper = m.public_helper.path if m.public_helper else None
        for f in public.files:
            stem = Path(f.path).stem
            if f.path.endswith("Test.java") and f.path != helper:
                cases.append({"id": pub_ids.get(stem, stem), "kind": "public"})
    return {
        "common_id": item.common_id,
        "revision": item.revision,
        "template_ref": m.ref,
        "template_dir": m.workspace,
        "locked": list(m.locked),
        "reserved": list(m.reserved),
        "test_command": TEST_COMMAND,
        "timeout_s": TIMEOUT_S,
        "classes": classes,
        "cases": cases,
    }


def node_config(t: Template, item: TaskMaterial) -> dict[str, Any]:
    """Node grade 설정 (image_grade_node) — 케이스 id 는 describe 제목(숨은: hc*, 공개: pt*)으로 판정이 찾는다."""
    m = t.manifest
    cmds = m.commands
    assert cmds is not None and cmds.test_compile and cmds.hidden_test
    cases = _hidden_cases(item)
    if item.public is not None:
        cases += [{"id": pt.test_id, "kind": "public"} for pt in item.contract.public_tests]
    return {
        "common_id": item.common_id,
        "revision": item.revision,
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


def py_config(t: Template, item: TaskMaterial) -> dict[str, Any]:
    """Python grade 설정 (image_grade_py) — Python 검증기와 같은 명령·대상 (케이스 id 는 테스트 이름의 hc*·pt*)."""
    m = t.manifest
    cases = _hidden_cases(item)
    if item.public is not None:
        cases += [{"id": pt.test_id, "kind": "public"} for pt in item.contract.public_tests]
    return {
        "common_id": item.common_id,
        "revision": item.revision,
        "template_ref": m.ref,
        "template_dir": m.workspace,
        "locked": list(m.locked),
        "reserved": list(m.reserved),
        "python": f"{PY_VENV}/bin/python",
        "compile_paths": list(python_verify.COMPILE_PATHS),
        "grading_paths": python_verify.grading_paths(t),
        "collect_paths": python_verify.collect_paths(t),
        "grading_ini": python_verify.GRADING_INI,
        "grading_rootdir": python_verify.GRADING_ROOTDIR,
        "layout": python_verify.layout_of(t),
        "case_id_pattern": m.hidden_rules.case_id_pattern,
        "timeout_s": TIMEOUT_S,
        "cases": cases,
    }


def _layer_files(tar: tarfile.TarFile, layer: str) -> list[str]:
    """묶음 안 레이어 하나(tar)의 파일 경로들."""
    with tarfile.open(fileobj=tar.extractfile(layer)) as lt:  # type: ignore[arg-type]
        return [m.name for m in lt.getmembers() if m.isfile()]


def _forbidden_in_layers(tar: tarfile.TarFile, layers: list[str], t: Template) -> list[str]:
    """레이어마다 금지 경로 — 런타임 경로(scan_skip)는 뺀다. 위 레이어에서 지워도 아래에 남으므로 레이어 단위로 본다."""
    rules, skip = candidate_layer_rules(t), stack_for(t).scan_skip
    bad: list[str] = []
    for layer in layers:
        bad += forbidden_in([p for p in _layer_files(tar, layer) if not p.startswith(skip)], rules)
    return sorted(set(bad))


def scan_candidates(archive: Path, targets: dict[str, Template]) -> dict[str, list[str]]:
    """저장한 묶음에서 응시용 이미지의 레이어마다 파일 이름을 꺼내 금지 경로를 찾는다.
    targets: 응시용 이미지 이름 → 그 과제의 템플릿 (금지 경로는 템플릿 manifest 에서). 다른 이미지(채점용)는 보지 않는다."""
    out: dict[str, list[str]] = {}
    with tarfile.open(archive) as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())  # type: ignore[union-attr]
        for entry in manifest:
            tags = entry.get("RepoTags") or []
            name = next((n for n in targets if any(tag.startswith(n) for tag in tags)), None)
            if name is not None:
                out[name] = _forbidden_in_layers(tar, entry["Layers"], targets[name])
    return out


# ── 만들기 단계 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Layers:
    """템플릿 하나의 공통 층 — 과제 이미지가 이 위에 얹힌다."""

    template: str  # 템플릿 층 이미지 (project/ + 오프라인 의존성)
    grader_base: str  # 채점 기반 이미지 (템플릿 층 + python3 + grade)


def build_layers(t: Template, ctx: Path, pm: Podman, log: Path, say: Progress = _quiet) -> Layers:
    """① 템플릿 층 ② 채점 기반 층 — 템플릿 판마다 한 번."""
    st = stack_for(t)
    stem = t.manifest.ref.replace("@", "-").replace("/", "-")  # backend-java-spring-boot3-jpa-v1
    template_img = f"localhost/task-template/{stem}"
    grader_base = f"localhost/task-grader-base/{stem}"
    say(f"① 템플릿 층 {t.manifest.ref} (project/ + 오프라인 의존성, {st.base})")
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
    say(f"② 채점 기반 층 {t.manifest.ref} (python3 + grade)")  # Python 런타임은 python3 설치 생략
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
    return Layers(template=template_img, grader_base=grader_base)


@dataclass
class TaskImages:
    """과제 하나의 응시용·채점용 이미지와 검증 결과."""

    item: TaskMaterial
    template: Template
    candidate: str  # 응시용 이미지 이름
    grader: str  # 채점용 이미지 이름
    tree: str  # 응시 작업 경로 tree_sha256
    cdir: Path  # 응시용 작업 폴더 (workspace/ — 검증 제출물의 바탕)
    config: dict[str, Any]  # 채점 설정 (config.json)
    grader_sha256: str
    offline_test: str | None = None  # 응시용 오프라인 기존 테스트 — pass / fail (<코드>)
    grades: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # 제출물 이름 → {seconds, result}


def build_task_images(
    t: Template, item: TaskMaterial, layers: Layers, ctx: Path, pm: Podman, log: Path
) -> TaskImages:
    """③ 과제 층 — 응시용(시작 코드 + README)·채점용(숨은 테스트·하네스 + config.json) 이미지."""
    cid, rev = item.common_id, item.revision
    cdir, kdir = ctx / item.key / "candidate", ctx / item.key / "grader"
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
        "ontology_version": item.ontology_version,
    }
    cand = f"localhost/task/{cid}-r{rev}-candidate"
    (cdir / "Containerfile").write_text(
        containerfile(
            layers.template,
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
            layers.grader_base,
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
    return TaskImages(
        item=item,
        template=t,
        candidate=cand,
        grader=grader,
        tree=tree,
        cdir=cdir,
        config=config,
        grader_sha256=grader_sha256(kdir),
    )


def verify_task_images(
    x: TaskImages, ctx: Path, samples: Path, pm: Podman, say: Progress = _quiet
) -> None:
    """④ 검증 — 응시용에서 네트워크 없이 기존 테스트, 채점용으로 시작 코드만·참조 구현 제출물 채점. 결과는 x 에 채운다.
    samples/<key>/ 에 로그·채점 결과를 남긴다 (단언 문구가 들어 있으므로 응시 쪽 전달물에 섞지 않는다)."""
    item = x.item
    say(f"④ 검증 {item.key}")
    sdir = samples / item.key
    sdir.mkdir(parents=True, exist_ok=True)
    # 응시용: 네트워크 없이 기존 테스트가 통과하는가 (의존성이 이미지 안에 다 있는가)
    res = pm.run(
        "--rm",
        "--network",
        "none",
        x.candidate,
        *stack_for(x.template).candidate_check,
        timeout=1800,
    )
    (sdir / "candidate-offline-test.log").write_text(
        res.stdout[-20000:] + res.stderr[-20000:], "utf-8"
    )
    x.offline_test = "pass" if res.returncode == 0 else f"fail ({res.returncode})"
    subs: dict[str, Overlay | None] = {
        "starter-only": None,
        **{k: v for k, v in item.refs.items() if v is not None},
    }
    x.grades = {}
    for name, ref in subs.items():
        sub = ctx / item.key / "submissions" / name
        if sub.exists():
            shutil.rmtree(sub)
        shutil.copytree(x.cdir / "workspace", sub)
        if ref is not None:
            write_overlay(ref, sub)
        res_dir = sdir / f"grade-{name}"
        if res_dir.exists():
            shutil.rmtree(res_dir)
        res_dir.mkdir(parents=True)
        g0 = time.monotonic()
        proc = pm.run(
            "--rm",
            "--network",
            "none",
            "-v",
            f"{sub}:/submission:ro,z",
            "-v",
            f"{res_dir}:/out:z",
            x.grader,
            "grade",
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
        x.grades[name] = {"seconds": round(time.monotonic() - g0, 1), "result": result}
        say(
            f"   {name}: {json.dumps((result or {}).get('summary'), ensure_ascii=False)} setup={(result or {}).get('setup')}"
        )


def save_archive(tasks: list[TaskImages], archive: Path, pm: Podman, log: Path) -> None:
    """⑤ 여러 이미지 한 묶음(docker-archive, 공통 레이어는 한 번)."""
    names = [n for x in tasks for n in (x.candidate, x.grader)]
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


def manifest_entry(x: TaskImages, pm: Podman, leaks: dict[str, list[str]]) -> dict[str, Any]:
    """manifest.json 의 과제 한 줄 — 이미지 id·크기·라벨, 응시·채점 명령, 검증 결과(케이스별 포함)."""
    item, t = x.item, x.template
    info: dict[str, dict[str, Any]] = {}
    for role, name in (("candidate", x.candidate), ("grader", x.grader)):
        insp = json.loads(pm("image", "inspect", name))[0]
        info[role] = {
            "name": name,
            "image_id": "sha256:" + insp["Id"].removeprefix("sha256:"),
            "size_mb": round(insp["Size"] / 1e6, 1),
            "labels": insp.get("Labels") or {},
        }
    return {
        "common_id": item.common_id,
        "revision": item.revision,
        "title": item.task.title,
        "source_build": item.source,
        "template_ref": t.manifest.ref,
        "base_image": stack_for(t).base,
        "candidate": {
            **info["candidate"],
            "workspace": t.manifest.workspace,
            "run": f"podman run -it --name <지원자> {x.candidate} bash",
            "commands": t.manifest.commands.model_dump() if t.manifest.commands else {},
            "offline_existing_tests": x.offline_test,
            "forbidden_paths_in_layers": leaks.get(x.candidate, []),
        },
        "grader": {
            **info["grader"],
            "grade": f"podman run --rm --network none -v <제출물>:/submission:ro -v <결과>:/out {x.grader} grade",
            "timeout_s": TIMEOUT_S,
            "cases": x.config["cases"],
            "grader_sha256": x.grader_sha256,
        },
        "verification": {
            name: {
                "seconds": g["seconds"],
                "setup": (g["result"] or {}).get("setup"),
                "summary": (g["result"] or {}).get("summary"),
                "cases": (g["result"] or {}).get("cases")
                or [],  # 케이스별 결과 — 빌드 화면 '테스트' 절
            }
            for name, g in x.grades.items()
        },
    }
