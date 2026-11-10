"""판정(grader) 이미지에 들어간 테스트 정보를 한 장의 HTML 로 본다 — 빌드 저장소(시험 DB)의 빌드마다.

정보: 이미지 안 `/grader/config.json`(실제 케이스 목록) · 숨은 계획(케이스 시나리오·통과 조건·의도한 사유) · 명세의 숨은 요구 ·
검증 결과(시작 코드·흔한 구현·허용 구현별 통과 수) · 숨은·공개 테스트 소스. 이미지의 케이스와 숨은 계획이 어긋나면 표시한다.

채점·평가 권한 전용 — 숨은 요구와 숨은 테스트가 평문으로 들어 있다. 산출물은 단일 HTML 파일(외부 요청 없음)이고 git 밖(data/)에 둔다.
인증 없는 API 화면에는 얹지 않는다 (API 응답 모델은 숨은 정보를 담지 않는다).

  uv run python -m eval.starter.grader_view --out data/mock_images/grader_view.html [--build <build_id> ...] [--db main]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg.rows import dict_row

from eval.starter.register_mock_common import dsn
from task_spec.config import Settings
from task_spec.starter.store import PgBuildStore

ROOT = Path(__file__).resolve().parents[2]


def image_config(image: str) -> dict[str, Any] | None:
    """판정 이미지 안의 /grader/config.json — 이미지가 없으면 None."""
    r = subprocess.run(
        [
            "podman",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "cat",
            image,
            "/grader/config.json",
        ],
        capture_output=True,
        text=True,
    )
    return json.loads(r.stdout) if r.returncode == 0 else None


def reasons_of(template_ref: str) -> dict[str, str]:
    m = ROOT / "templates" / template_ref.replace("@", "/") / "manifest.yaml"
    if not m.is_file():
        return {}
    return {
        r["code"]: r["description"]
        for r in yaml.safe_load(m.read_text("utf-8")).get("failure_reasons", [])
    }


def case_rows(cfg: dict[str, Any] | None, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """이미지 케이스(진실) + 숨은 계획의 설명. 계획에만 있거나 이미지에만 있는 케이스는 mismatch."""
    planned = {c["case_id"]: c for c in plan.get("cases", [])}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in (cfg or {}).get("cases", []):
        p = planned.get(c["id"])
        seen.add(c["id"])
        rows.append(
            {
                "id": c["id"],
                "kind": c["kind"],
                "trap_id": c.get("trap_id"),
                "intended_reason": c.get("intended_reason"),
                "ac_id": p and p["ac_id"],
                "scenario": p and p["scenario"],
                "expect": p and p["expect"],
                "setup": (p or {}).get("setup", []),
                "mismatch": c["kind"] == "hidden" and p is None,
            }
        )
    for cid, p in planned.items():
        if cid not in seen:
            rows.append(
                {
                    "id": cid,
                    "kind": "hidden",
                    "trap_id": p["trap_id"],
                    "intended_reason": p["intended_reason"],
                    "ac_id": p["ac_id"],
                    "scenario": p["scenario"],
                    "expect": p["expect"],
                    "setup": p.get("setup", []),
                    "mismatch": True,
                    "missing_in_image": True,
                }
            )
    return rows


async def collect(conninfo: str, only: list[str]) -> list[dict[str, Any]]:
    store = await PgBuildStore.open(conninfo)
    out: list[dict[str, Any]] = []
    try:
        async with await psycopg.AsyncConnection.connect(conninfo, row_factory=dict_row) as conn:
            cur = await conn.execute("SELECT id, task FROM public.common_task")
            tasks = {r["id"]: r["task"] for r in await cur.fetchall()}
            cur = await conn.execute(
                "SELECT id FROM public.common_task_build ORDER BY created_at, id"
            )
            ids = [r["id"] for r in await cur.fetchall()]
        for bid in ids:
            if only and bid not in only:
                continue
            b = await store.get(bid)
            if b is None or b.outcome is None:
                continue
            o = b.outcome
            arts = {a.role: a for a in await store.artifacts(bid)}
            g = arts.get("grader")
            image = f"localhost/task/{bid}-grader"
            cfg = image_config(image)
            plan = o.hidden_plan.model_dump(mode="json")
            task = tasks.get(b.common_id, {})
            reasons = reasons_of(o.template_ref)
            files = [
                {"kind": f.kind, "path": f.path, "content": f.content}
                for f in await store.files(bid, ["hidden", "public_test"])
            ]
            out.append(
                {
                    "build_id": bid,
                    "common_id": b.common_id,
                    "status": b.status,
                    "title": task.get("title"),
                    "template_ref": o.template_ref,
                    "tree_sha256": o.tree_sha256,
                    "grader_sha256": o.grader_sha256,
                    "image": image,
                    "image_id": g.image_id if g else None,
                    "image_found": cfg is not None,
                    "image_files": [{"name": f.name, "size": f.size_bytes} for f in g.files]
                    if g
                    else [],
                    "timeout_s": (cfg or {}).get("timeout_s"),
                    "test_command": (cfg or {}).get("test_command"),
                    "harness": plan.get("harness"),
                    "cases": case_rows(cfg, plan),
                    "traps": [
                        {"trap_id": t["trap_id"], "description": t["description"]}
                        for t in task.get("traps", [])
                    ],
                    "acceptance_hidden": task.get("acceptance_hidden", []),
                    "reasons": {
                        c["intended_reason"]: reasons.get(c["intended_reason"], "")
                        for c in plan.get("cases", [])
                    },
                    "verification": {
                        k: v.model_dump(mode="json") for k, v in o.verification.items()
                    },
                    "review_flags": o.review_flags,
                    "repairs": len(o.repairs),
                    "files": files,
                }
            )
    finally:
        await store.close()
    return out


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>판정 이미지 테스트 정보</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#1c2128;--mute:#5d6673;--line:#dfe3e8;--acc:#2456c7;--ok:#17803d;--bad:#b42318;--warn:#a15c00;--code:#f1f3f6}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--panel:#1c2027;--ink:#e6e9ee;--mute:#98a2b0;--line:#2e3540;--acc:#7aa2ff;--ok:#4cc27a;--bad:#ff7b72;--warn:#e0a24a;--code:#12151a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);background:var(--panel);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:16px;margin:0}.badge{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--bad);color:var(--bad)}
main{display:grid;grid-template-columns:260px 1fr;min-height:calc(100vh - 54px)}
nav{border-right:1px solid var(--line);background:var(--panel);padding:8px;overflow:auto}
nav button{display:block;width:100%;text-align:left;border:0;background:none;color:inherit;padding:9px 10px;border-radius:8px;cursor:pointer;font:inherit}
nav button:hover{background:var(--code)}nav button[aria-current=true]{background:var(--code);box-shadow:inset 3px 0 var(--acc)}
nav small{display:block;color:var(--mute)}section{padding:20px;max-width:1100px}
h2{font-size:18px;margin:0 0 4px}h3{font-size:14px;margin:22px 0 8px}.mute{color:var(--mute)}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;margin:12px 0}.meta div{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 10px;overflow-wrap:anywhere}
.meta b{display:block;font-size:12px;color:var(--mute);font-weight:500}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--mute);font-weight:600;background:var(--code)}
tr:last-child td{border-bottom:0}.num{text-align:right;font-variant-numeric:tabular-nums}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}code,pre{font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
code{background:var(--code);padding:1px 5px;border-radius:4px}pre{background:var(--code);padding:12px;border-radius:8px;overflow:auto;margin:0;max-height:480px}
details{background:var(--panel);border:1px solid var(--line);border-radius:8px;margin:6px 0}summary{cursor:pointer;padding:8px 12px}details pre{border-radius:0 0 8px 8px}
.tag{display:inline-block;font-size:12px;padding:1px 7px;border-radius:99px;background:var(--code);border:1px solid var(--line)}
@media (max-width:760px){main{grid-template-columns:1fr}nav{border-right:0;border-bottom:1px solid var(--line);display:flex;overflow-x:auto}nav button{white-space:nowrap}section{padding:16px}}
</style></head><body>
<header><h1>판정 이미지 테스트 정보</h1><span class="badge">채점·평가 권한 전용 — 숨은 요구·숨은 테스트 포함</span><span class="mute" id="gen"></span></header>
<main><nav id="nav"></nav><section id="view"></section></main>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const $=(s,r=document)=>r.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const mb=n=>(n/1e6).toFixed(1)+' MB';
$('#gen').textContent=D.generated+' · '+D.builds.length+'개 빌드';
const nav=$('#nav');
D.builds.forEach((b,i)=>{const e=document.createElement('button');e.innerHTML=esc(b.title||b.build_id)+'<small>'+esc(b.build_id)+' · '+esc(b.template_ref.split('@')[0])+'</small>';e.onclick=()=>show(i);nav.append(e)});
function pass(s){return s.hidden_total?`${s.hidden_passed}/${s.hidden_total}`:'–'}
function show(i){
 [...nav.children].forEach((e,j)=>e.setAttribute('aria-current',j===i));
 const b=D.builds[i];const hid=b.cases.filter(c=>c.kind==='hidden'),pub=b.cases.filter(c=>c.kind==='public');
 const mism=b.cases.filter(c=>c.mismatch);
 let h=`<h2>${esc(b.title||b.common_id)}</h2><div class="mute">${esc(b.build_id)} · <span class="tag">${esc(b.status)}</span> · ${esc(b.template_ref)}</div>
 <div class="meta">
 <div><b>판정 이미지</b>${esc(b.image)}${b.image_found?'':' <span class="bad">(로컬에 없음)</span>'}</div>
 <div><b>image_id</b><code>${esc((b.image_id||'').slice(0,19))}</code></div>
 <div><b>채점 내용 해시 (grader_sha256)</b><code>${esc(b.grader_sha256.slice(0,16))}</code></div>
 <div><b>시작 코드 해시 (tree_sha256)</b><code>${esc(b.tree_sha256.slice(0,16))}</code></div>
 <div><b>제한 시간 · 실행 명령</b>${esc(b.timeout_s)}초 · <code>${esc(b.test_command)}</code></div>
 <div><b>이미지 안 케이스</b>숨은 ${hid.length}개 · 공개 ${pub.length}개</div>
 <div><b>수리 이력 · 검토 표시</b>${b.repairs}건 · ${b.review_flags.length}건</div>
 <div><b>파일</b>${b.image_files.map(f=>esc(f.name)+' ('+mb(f.size)+')').join('<br>')}</div></div>`;
 if(mism.length)h+=`<p class="bad">이미지 케이스와 숨은 계획이 어긋납니다: ${mism.map(c=>esc(c.id)+(c.missing_in_image?' (이미지에 없음)':' (계획에 없음)')).join(', ')}</p>`;
 h+=`<h3>숨은 케이스 — 흔한 구현을 잡는 테스트</h3><table><tr><th>케이스</th><th>숨은 요구</th><th>의도한 실패 사유</th><th>시나리오 → 통과 조건</th></tr>`;
 hid.forEach(c=>{const t=b.traps.find(t=>t.trap_id===c.trap_id);
  h+=`<tr><td><b>${esc(c.id)}</b><br><span class="mute">${esc(c.ac_id||'')}</span></td><td><code>${esc(c.trap_id)}</code><br>${esc(t?t.description:'')}</td>
  <td><code>${esc(c.intended_reason)}</code><br><span class="mute">${esc(b.reasons[c.intended_reason]||'')}</span></td>
  <td>${esc(c.scenario||'')}<br><b>→</b> ${esc(c.expect||'')}${(c.setup||[]).length?'<br><span class="mute">준비: '+esc(c.setup.join(' / '))+'</span>':''}</td></tr>`});
 h+=`</table><h3>검증 결과 — 판정 이미지로 채점한 구현별 통과 수</h3><table><tr><th>구현</th><th class="num">숨은</th><th class="num">공개</th><th>기대</th></tr>`;
 Object.entries(b.verification).forEach(([k,s])=>{
  const naive=k.startsWith('naive'),start=k==='starter-only';
  const okExp=(naive||start)?s.hidden_passed===0:s.hidden_passed===s.hidden_total;
  h+=`<tr><td>${esc(k)}</td><td class="num ${okExp?'ok':'bad'}">${pass(s)}</td><td class="num">${s.public_passed}/${s.public_total}</td><td class="mute">${naive?'흔한 구현 — 숨은 테스트에서 실패':start?'시작 코드 — 숨은 테스트에서 실패':'허용 설계 — 모두 통과'}</td></tr>`});
 h+=`</table>`;
 if(b.harness)h+=`<h3>하네스 조절값</h3><pre>${esc(JSON.stringify(b.harness,null,1))}</pre>`;
 h+=`<h3>공개 케이스 (지원자가 보는 테스트)</h3><div>${pub.map(c=>'<span class="tag">'+esc(c.id)+'</span>').join(' ')||'–'}</div>`;
 if(b.review_flags.length)h+=`<h3 class="warn">승인자가 볼 검토 표시</h3><ul>${b.review_flags.map(f=>'<li>'+esc(f)+'</li>').join('')}</ul>`;
 h+=`<h3>테스트 소스 (이미지에 들어간 파일)</h3>`+b.files.map(f=>`<details><summary><span class="tag">${esc(f.kind==='hidden'?'숨은':'공개')}</span> <code>${esc(f.path)}</code> <span class="mute">${f.content.split('\\n').length}줄</span></summary><pre>${esc(f.content)}</pre></details>`).join('');
 $('#view').innerHTML=h;window.scrollTo(0,0);location.hash=b.build_id}
show(Math.max(0,D.builds.findIndex(b=>b.build_id===location.hash.slice(1))));
</script></body></html>
"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--build", action="append", default=[], help="빌드 id (없으면 전부)")
    ap.add_argument("--db", choices=["test", "main"], default="test")
    a = ap.parse_args()
    s = Settings()  # type: ignore[call-arg]
    builds = await collect(dsn(s, a.db), a.build)
    from datetime import datetime

    data = json.dumps(
        {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "builds": builds},
        ensure_ascii=False,
    )
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(PAGE.replace("__DATA__", data.replace("</", "<\\/")), "utf-8")
    out.chmod(0o600)
    print(f"→ {out} ({len(builds)}개 빌드, {out.stat().st_size / 1e3:.0f} KB)")
    for b in builds:
        bad = [c["id"] for c in b["cases"] if c["mismatch"]]
        print(
            f"  {b['build_id']}: 숨은 {sum(c['kind'] == 'hidden' for c in b['cases'])} · 공개 {sum(c['kind'] == 'public' for c in b['cases'])} · 이미지 {'있음' if b['image_found'] else '없음'}"
            + (f" · 어긋남 {bad}" if bad else "")
        )


if __name__ == "__main__":
    asyncio.run(main())
