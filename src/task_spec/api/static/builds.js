/* 공통 과제 빌드 — 임시 화면 (api/routers/ui.py 가 /ui/builds.html 로 내준다).
   기존 API(/builds · /builds/{id} · /cases · /artifacts · /deliveries · /delivery-targets)만 부른다. 과제 생성 요청은 실행 화면(app.js)에서,
   빌드 승인(#130)은 /builds/{id}/approval·approve, 실패 빌드 다시 요청은 /common-tasks/{id}/builds.
   '테스트' 절은 문제를 쓸지 정하는 승인자용 — 숨은 케이스의 상황·통과 조건·의도한 사유·허용 설계와 대상별 결과 (테스트 코드는 없다). */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

class ApiError extends Error {}
async function api(path) {
  let res;
  try {
    res = await fetch(path, { headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError("서버에 연결하지 못했습니다.");
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* 본문 없음 */
  }
  if (!res.ok) throw new ApiError((data && data.error && data.error.message) || `요청이 실패했습니다 (HTTP ${res.status})`);
  return data;
}

const STATUS = {
  pending: "대기",
  generating: "생성 중",
  verifying: "검증 중",
  review: "검토",
  approved: "승인",
  published: "전달됨",
  failed: "실패",
};
const ROLE = { candidate: "응시용", grader: "채점용", eval_kit: "평가 키트" };
const VERDICT = { confirmed: "확인", fixed: "고침", accepted_risk: "알고 받아들임" };
const pill = (s) => `<span class="pill b-${esc(s)}">${esc(STATUS[s] || s)}</span>`;
const stack = (ref) => (ref || "").split("@")[0].split("/").pop() || "–";
const when = (iso) => (iso ? new Date(iso).toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "short" }) : "–");
const mb = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${(n / 1e3).toFixed(1)} KB`);

let builds = [];
let filter = "";
let current = null;

function renderList() {
  const rows = builds.filter((b) => !filter || b.status === filter);
  $("#list-count").textContent = `${rows.length} / ${builds.length}`;
  $("#list-empty").hidden = rows.length > 0;
  $("#list").innerHTML = rows
    .map(
      (b) => `<li><button type="button" data-id="${esc(b.build_id)}" aria-current="${b.build_id === current}">
        <span><span class="recent-name">${esc(b.title || b.common_id)}</span>
        <span class="sub mono">${esc(b.build_id)} · ${esc(stack(b.template_ref))} · ${esc(b.posting_id || "")}</span></span>
        ${pill(b.status)}${b.stage && ["generating", "verifying"].includes(b.status) ? `<span class="sub">${esc(b.stage)}</span>` : ""}</button></li>`,
    )
    .join("");
}

async function loadList() {
  $("#list-count").textContent = "읽는 중…";
  try {
    builds = await api("/builds?limit=200");
    renderList();
    const want = decodeURIComponent(location.hash.slice(1));
    if (want && builds.some((b) => b.build_id === want)) show(want);
  } catch (e) {
    $("#list-count").textContent = e.message;
  }
}

/* 검증 대상별 기대: 시작 코드·흔한 구현은 숨은 테스트에서 떨어지고, 허용 구현은 모두 통과해야 한다 */
function expectOf(name) {
  if (name === "starter-only") return { label: "시작 코드", fail: true };
  if (name.startsWith("naive")) return { label: "흔한 구현", fail: true };
  return { label: "허용 구현", fail: false };
}

function verificationTable(v) {
  const names = Object.keys(v).sort((a, b) => {
    const r = (n) => (n === "starter-only" ? 0 : n.startsWith("naive") ? 1 : 2);
    return r(a) - r(b) || a.localeCompare(b);
  });
  if (!names.length) return `<p class="placeholder">검증 결과가 없습니다.</p>`;
  const rows = names
    .map((n) => {
      const s = v[n];
      const e = expectOf(n);
      const ok = e.fail ? s.hidden_passed === 0 : s.hidden_passed === s.hidden_total;
      const pubOk = e.label === "시작 코드" || s.public_passed === s.public_total;
      const extra = [...(s.existing_failed || []).map((t) => `기존 ${t}`), ...(s.selftest_failed || []).map((t) => `자체 점검 ${t}`)];
      return `<tr><td><b>${esc(n)}</b> <span class="faint">${e.label}</span></td>
        <td class="num ${ok ? "good" : "bad"}">${s.hidden_passed}/${s.hidden_total}</td>
        <td class="num ${pubOk ? "" : "bad"}">${s.public_passed}/${s.public_total}</td>
        <td class="faint">${e.fail ? "숨은 테스트에서 떨어져야 함" : "모두 통과해야 함"}${extra.length ? `<br><span class="bad">${esc(extra.join(", "))}</span>` : ""}</td></tr>`;
    })
    .join("");
  return `<div class="tbl-wrap"><table><thead><tr><th>대상</th><th class="num">숨은</th><th class="num">공개</th><th>기대</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

/* 테스트 — 케이스마다 무엇을 확인하나 + 대상별 결과 (✓ 통과 · ✗ 실패 사유). 시작 코드·흔한 구현은 숨은 케이스에서 떨어져야 한다 */
function casesBlock(c) {
  if (!c) return `<p class="placeholder">테스트 정보를 읽지 못했습니다.</p>`;
  const targets = Object.keys(c.results).sort((a, b) => {
    const r = (n) => (n === "starter-only" ? 0 : n.startsWith("naive") ? 1 : 2);
    return r(a) - r(b) || a.localeCompare(b);
  });
  const hasResults = targets.some((t) => c.results[t].length);
  const cell = (t, cs) => {
    const r = (c.results[t] || []).find((x) => x.id === cs.id || x.id.toLowerCase().startsWith(cs.id));
    if (!r) return `<td class="faint">–</td>`;
    const want = cs.kind === "hidden" && expectOf(t).fail ? false : true; // 시작 코드·흔한 구현은 숨은 케이스에서 떨어져야
    const good = t === "starter-only" && cs.kind === "public" ? true : r.passed === want;
    const intended = !r.passed && cs.intended_reason && r.reason === cs.intended_reason;
    const label = r.passed ? "✓" : `✗${r.reason ? ` ${esc(r.reason)}` : ""}${intended ? " (의도)" : ""}`;
    return `<td class="${good ? "good" : "bad"}" title="${esc(r.message || "")}">${label}</td>`;
  };
  const head = `<tr><th>케이스</th><th>무엇을 확인하나</th>${hasResults ? targets.map((t) => `<th>${esc(t)}<br><span class="faint">${expectOf(t).label}</span></th>`).join("") : ""}</tr>`;
  const rows = c.cases
    .map((cs) => {
      const what =
        cs.kind === "hidden"
          ? `${esc(cs.scenario || "")}<br><b>통과:</b> ${esc(cs.expect)}${cs.intended_reason ? `<br><span class="faint">의도한 실패 사유</span> <span class="mono">${esc(cs.intended_reason)}</span>` : ""}${cs.setup.length ? `<br><span class="faint">하네스 준비: ${esc(cs.setup.join(" · "))}</span>` : ""}`
          : esc(cs.expect);
      return `<tr id="case-${esc(cs.id)}"><td><b class="mono">${esc(cs.id)}</b><br><span class="faint">${cs.kind === "hidden" ? "숨은" : "공개"} · ${esc(cs.ac_id)}${cs.trap_id ? ` · ${esc(cs.trap_id)}` : ""}</span></td>
        <td>${what}</td>${hasResults ? targets.map((t) => cell(t, cs)).join("") : ""}</tr>`;
    })
    .join("");
  // 계약 케이스 id 와 맞지 않는 결과 (Java 공개 테스트는 클래스 이름 — 예: AuditLogDescendingOrderTest)
  const known = (id) => c.cases.some((cs) => id === cs.id || id.toLowerCase().startsWith(cs.id));
  const others = [...new Set(targets.flatMap((t) => c.results[t].map((r) => r.id)).filter((id) => !known(id)))];
  const otherRows = others
    .map((id) => {
      const kind = targets.map((t) => c.results[t].find((r) => r.id === id)).find(Boolean)?.kind || "";
      return `<tr><td><b class="mono">${esc(id)}</b><br><span class="faint">${kind === "hidden" ? "숨은" : "공개"} · 계약 id 없음</span></td><td class="faint">–</td>${targets
        .map((t) => {
          const r = c.results[t].find((x) => x.id === id);
          return r ? `<td class="${r.passed ? "good" : "bad"}" title="${esc(r.message || "")}">${r.passed ? "✓" : `✗${r.reason ? ` ${esc(r.reason)}` : ""}`}</td>` : `<td class="faint">–</td>`;
        })
        .join("")}</tr>`;
    })
    .join("");
  const acs = [...c.acceptance_hidden.map((a) => ["숨김", a]), ...c.acceptance_public.map((a) => ["공개", a])]
    .map(([k, a]) => `<li><span class="mono">${esc(a.ac_id)}</span><span class="faint">${k}${a.trap_id ? ` · ${esc(a.trap_id)}` : ""}</span><p>${esc(a.text)}</p></li>`)
    .join("");
  const designs = c.accepted_designs
    .map((d) => `<li><span class="mono">${esc(d.design_id)}</span><span class="faint">${esc(d.trap_ids.join(", "))}</span><p>${esc(d.summary)}<br><span class="faint">${esc(d.conditions)}</span></p></li>`)
    .join("");
  return `<div class="tbl-wrap"><table><thead>${head}</thead><tbody>${rows}${otherRows}</tbody></table></div>
    ${hasResults ? "" : `<p class="placeholder">이 빌드는 케이스별 결과를 저장하기 전에 적재돼 결과 칸이 없습니다.</p>`}
    <div class="eyebrow">수용 기준</div><ul class="bflags">${acs || "<li>없음</li>"}</ul>
    <div class="eyebrow">허용 설계 (통과로 인정하는 구현 방향)</div><ul class="bflags">${designs || "<li>없음</li>"}</ul>`;
}

function flagsBlock(flags) {
  if (!flags.length) return `<p class="placeholder">검토 표시가 없습니다.</p>`;
  return `<ul class="bflags">${flags
    .map(
      (f) => `<li><span class="mono faint">${esc(f.flag_id)}</span>
      ${f.verdict ? `<span class="pill b-approved">${esc(VERDICT[f.verdict] || f.verdict)}</span>` : `<span class="pill">판단 전</span>`}
      <p>${esc(f.text)}</p></li>`,
    )
    .join("")}</ul>`;
}

function artifactsBlock(arts) {
  if (!arts.length) return `<p class="placeholder">등록된 묶음이 없습니다.</p>`;
  return arts
    .map((a) => {
      const files = a.files
        .map((f) => `<tr><td class="mono">${esc(f.name)}</td><td class="num">${mb(f.size_bytes)}</td>
          <td class="mono hash"><span title="${esc(f.sha256)}">${esc(f.sha256.slice(0, 16))}…</span>
          <button type="button" class="btn small copy" data-copy="${esc(f.sha256)}">복사</button></td></tr>`)
        .join("");
      return `<div class="art"><div class="block-head"><h4>${esc(ROLE[a.role] || a.role)}</h4>
        ${a.image_id ? `<span class="aside mono">image_id ${esc(a.image_id.slice(0, 19))}… <button type="button" class="btn small copy" data-copy="${esc(a.image_id)}">복사</button></span>` : ""}
        <span class="aside">${when(a.created_at)}</span></div>
        <div class="tbl-wrap"><table><thead><tr><th>파일</th><th class="num">크기</th><th>sha256</th></tr></thead><tbody>${files}</tbody></table></div></div>`;
    })
    .join("");
}

function deliveriesBlock(ds, targets) {
  const t = targets.length
    ? `<div class="chips">${targets.map((x) => `<span class="chip">${esc(x.name)} → ${esc(ROLE[x.role] || x.role)}</span>`).join("")}</div>`
    : `<p class="placeholder">등록된 전달 대상이 없습니다.</p>`;
  const d = ds.length
    ? `<div class="tbl-wrap"><table><thead><tr><th>대상</th><th>역할</th><th>상태</th><th>요청</th><th>보냄</th></tr></thead><tbody>${ds
        .map((x) => `<tr><td class="mono">${esc(x.target)}</td><td>${esc(ROLE[x.role] || x.role)}</td><td>${esc(x.status)}${x.error ? `<br><span class="bad">${esc(x.error)}</span>` : ""}</td><td>${esc(x.requested_by)}<br><span class="faint">${when(x.requested_at)}</span></td><td>${when(x.sent_at)}</td></tr>`)
        .join("")}</tbody></table></div>`
    : `<p class="placeholder">전달 기록이 없습니다. 전달은 승인된 빌드만 할 수 있습니다.</p>`;
  return `<div class="eyebrow">전달 대상</div>${t}<div class="eyebrow">전달 기록</div>${d}`;
}

/* 빌드 승인 (#130) — GET 이 승인 thread 를 시작, POST 는 resume 만. 판단이 빠지면 서버가 다시 기다리고 사유를 준다 */
async function send(path, json) {
  let res;
  try {
    res = await fetch(path, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(json),
    });
  } catch {
    throw new ApiError("서버에 연결하지 못했습니다.");
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* 본문 없음 */
  }
  if (!res.ok) throw new ApiError((data && data.error && data.error.message) || `요청이 실패했습니다 (HTTP ${res.status})`);
  return data;
}
const remembered = {
  get() {
    try {
      return localStorage.getItem("taskspec.reviewer") || "";
    } catch {
      return "";
    }
  },
  set(v) {
    try {
      localStorage.setItem("taskspec.reviewer", v);
    } catch {
      /* 저장 못 해도 동작에는 영향 없음 */
    }
  },
};
const say = (text, kind = "") => {
  const m = $("#approve-msg");
  if (m) {
    m.textContent = text;
    m.className = `msg ${kind}`;
  }
};

async function renderApproval(b) {
  const box = $("#approve-body");
  try {
    const v = await api(`/builds/${encodeURIComponent(b.build_id)}/approval`);
    if (current !== b.build_id || !box.isConnected) return;
    if (v.state !== "awaiting") {
      box.innerHTML = `<p class="placeholder">승인 대기가 아닙니다 (${esc(v.state)}).</p>`;
      return;
    }
    const opts = Object.entries(VERDICT).map(([k, label]) => `<option value="${k}">${esc(label)}</option>`).join("");
    box.innerHTML = `<form id="approve-form">
      ${v.error ? `<p class="bad">${esc(v.error)}</p>` : ""}
      ${
        v.flags.length
          ? `<ul class="bflags">${v.flags
              .map(
                (f, i) => `<li><p>${esc(f.text)}</p>
              <div class="row" style="gap:8px;flex-wrap:wrap"><select data-flag="${i}" aria-label="판단"><option value="">판단 선택</option>${opts}</select>
              <input type="text" data-note="${i}" placeholder="메모 (선택)" style="flex:1;min-width:160px"></div></li>`,
              )
              .join("")}</ul>`
          : `<p class="placeholder">검토 표시가 없습니다 — 테스트 절을 보고 승인하세요.</p>`
      }
      <div class="row" style="gap:8px;align-items:center;margin:10px 0"><label for="approve-by">승인자</label>
        <input type="text" id="approve-by" value="${esc(remembered.get())}" autocomplete="name" style="max-width:200px"></div>
      <textarea id="approve-comment" placeholder="메모 — 반려할 때는 사유가 필요합니다"></textarea>
      <div class="actions" style="margin-top:10px;display:flex;gap:8px">
        <button type="button" class="btn primary" data-act="approved">승인</button>
        <button type="button" class="btn" data-act="rejected">반려</button></div>
      <p class="msg" id="approve-msg" role="status"></p></form>`;
    box.querySelector("#approve-form").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-act]");
      if (btn) submitApproval(b, v.flags, btn);
    });
  } catch (e) {
    box.innerHTML = `<p class="bad">${esc(e.message)}</p>`;
  }
}

async function submitApproval(b, flags, btn) {
  const status = btn.dataset.act;
  const reviewer = $("#approve-by").value.trim();
  const comment = $("#approve-comment").value.trim();
  if (!reviewer) return say("승인자 이름을 적으세요.", "err");
  if (status === "rejected" && !comment) return say("반려에는 사유가 필요합니다.", "err");
  const decisions = flags.map((f, i) => ({
    flag_id: f.flag_id,
    flag: f.text,
    verdict: $(`select[data-flag="${i}"]`).value,
    note: $(`input[data-note="${i}"]`).value.trim(),
    reviewer,
  }));
  if (status === "approved" && decisions.some((d) => !d.verdict)) return say("검토 표시마다 판단을 고르세요.", "err");
  remembered.set(reviewer);
  btn.disabled = true;
  try {
    const r = await send(`/builds/${encodeURIComponent(b.build_id)}/approve`, {
      status,
      reviewer,
      at: new Date().toISOString(),
      decisions: status === "approved" ? decisions : [],
      comment: comment || null,
    });
    say(r.state === "approved" ? "승인했습니다. 묶음 확정(평가 키트)은 builder 가 이어서 합니다." : "반려했습니다.", "ok");
    loadList();
    setTimeout(() => show(b.build_id), 600);
  } catch (e) {
    say(e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

function renderRequestAgain(b) {
  const box = $("#approve-body");
  box.innerHTML = `<div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">
      <label for="again-by">요청자</label><input type="text" id="again-by" value="${esc(remembered.get())}" style="max-width:200px">
      <button type="button" class="btn" id="again-btn">같은 공통 과제로 다시 요청</button></div>
    <p class="msg" id="approve-msg" role="status"></p>`;
  $("#again-btn").addEventListener("click", async (ev) => {
    const by = $("#again-by").value.trim();
    if (!by) return say("요청자 이름을 적으세요.", "err");
    remembered.set(by);
    ev.target.disabled = true;
    try {
      const r = await send(`/common-tasks/${encodeURIComponent(b.common_id)}/builds`, { requested_by: by });
      say(r.created ? `요청했습니다 — ${r.build.build_id}` : `이미 진행 중인 빌드가 있습니다 — ${r.build.build_id}`, "ok");
      loadList();
    } catch (e) {
      say(e.message, "err");
    } finally {
      ev.target.disabled = false;
    }
  });
}

/* 큐 (#128) — 생성 요청으로 만든 빌드만: 요청자·단계·시도·비용·실패 사유 */
function queueLine(b) {
  if (!b.requested_by) return "";
  const approved = b.status === "approved" || b.status === "published";
  const parts = [`<span>요청 ${esc(b.requested_by)} · ${when(b.requested_at)}</span>`];
  if (b.stage) parts.push(`<span>단계 ${esc(b.stage)}</span>`);
  if (b.attempt > 1) parts.push(`<span>시도 ${b.attempt}회</span>`);
  if (b.cost_usd) parts.push(`<span>비용 $${Number(b.cost_usd).toFixed(2)}</span>`);
  if (b.finished_at) parts.push(`<span>끝 ${when(b.finished_at)}</span>`);
  // 묶음 확정 (#130) — 승인 뒤 builder 가 descriptor 를 approved 로 다시 쓰고 평가 키트를 등록해야 전달할 수 있다
  if (approved) parts.push(`<span>${b.packages_final_at ? `묶음 확정 ${when(b.packages_final_at)}` : "묶음 확정 대기 (builder)"}</span>`);
  const err = b.error ? `<p class="bad" style="margin:6px 0 0">${esc(b.error === "superseded" ? "대기 중에 공고의 새 판이 승인되어 만들지 않았습니다 (superseded)" : b.error)}</p>` : "";
  return `<div class="meta">${parts.join("")}</div>${err}`;
}

async function show(id) {
  current = id;
  location.hash = encodeURIComponent(id);
  renderList();
  $("#empty").hidden = true;
  const el = $("#detail");
  el.hidden = false;
  el.innerHTML = `<div class="sheet-head"><p class="placeholder">읽는 중…</p></div>`;
  try {
    const enc = encodeURIComponent(id);
    const [b, cases, arts, ds, targets] = await Promise.all([
      api(`/builds/${enc}`),
      api(`/builds/${enc}/cases`).catch(() => null),
      api(`/builds/${enc}/artifacts`),
      api(`/builds/${enc}/deliveries`),
      api("/delivery-targets"),
    ]);
    if (current !== id) return; // 그 사이 다른 빌드를 골랐다
    const decided = b.review_flags.filter((f) => f.verdict).length;
    const files = Object.entries(b.files).map(([k, n]) => `${k} ${n}`).join(" · ") || "–";
    el.innerHTML = `
      <div class="sheet-head"><div>
        <div class="eyebrow">${esc(b.posting_id || "")}</div>
        <h2>${esc(b.title || b.common_id)}</h2>
        <div class="meta"><span class="mono">${esc(b.build_id)}</span><span>${esc(b.template_ref || "–")}</span><span>만든 시각 ${when(b.created_at)}</span>
        ${b.approved_by ? `<span>승인 ${esc(b.approved_by)} · ${when(b.approved_at)}</span>` : ""}</div>
        ${queueLine(b)}</div>
        <div class="right">${pill(b.status)}<button type="button" class="btn small" data-problem>문제 보기</button></div></div>
      <nav class="jump" aria-label="빌드 안에서 이동"><a href="#" data-jump="b-verify">검증</a><a href="#" data-jump="b-cases">테스트</a><a href="#" data-jump="b-flags">검토 표시</a>${b.status === "review" || b.status === "failed" ? `<a href="#" data-jump="b-approve">${b.status === "review" ? "승인" : "다시 요청"}</a>` : ""}<a href="#" data-jump="b-arts">이미지 묶음</a><a href="#" data-jump="b-deliv">전달</a></nav>
      <section class="block" id="b-verify"><div class="block-head"><h3>검증 결과</h3><span class="aside">채점용 이미지로 채점한 구현별 통과 수</span></div>
        ${verificationTable(b.verification)}
        <div class="kv"><span>오버레이 파일</span><b>${esc(files)}</b><span>수리</span><b>${b.repairs}건</b>
        <span>시작 코드 해시</span><b class="mono">${esc((b.tree_sha256 || "–").slice(0, 16))}</b><span>채점 내용 해시</span><b class="mono">${esc((b.grader_sha256 || "–").slice(0, 16))}</b>
        <span>온톨로지</span><b class="mono">${esc(b.ontology_version)}</b></div></section>
      <section class="block" id="b-cases"><div class="block-head"><h3>테스트</h3><span class="aside">문제를 쓸지 정할 때 — 케이스마다 무엇을 확인하고 누가 통과했나 (칸에 마우스를 올리면 실패 메시지)</span></div>${casesBlock(cases)}</section>
      <section class="block" id="b-flags"><div class="block-head"><h3>검토 표시</h3><span class="aside">승인 전에 표시마다 승인자 판단이 필요합니다 · ${decided}/${b.review_flags.length} 판단</span></div>${flagsBlock(b.review_flags)}</section>
      ${
        b.status === "review" || b.status === "failed"
          ? `<section class="block" id="b-approve"><div class="block-head"><h3>${b.status === "review" ? "승인" : "다시 요청"}</h3>
              <span class="aside">${b.status === "review" ? "시작 코드·테스트·이미지를 쓸지 정합니다 — 표시마다 판단한 뒤 승인 또는 반려" : "실패·반려된 빌드는 같은 공통 과제로 새로 만들 수 있습니다 (LLM 비용 발생)"}</span></div>
              <div id="approve-body"><p class="placeholder">읽는 중…</p></div></section>`
          : ""
      }
      <section class="block" id="b-arts"><div class="block-head"><h3>이미지 묶음</h3><span class="aside">받는 곳별 폴더 — 응시용은 응시 환경, 채점용은 채점 서버로만</span></div>${artifactsBlock(arts)}</section>
      <section class="block" id="b-deliv"><div class="block-head"><h3>전달</h3></div>${deliveriesBlock(ds, targets)}</section>`;
    if (b.status === "review") renderApproval(b);
    else if (b.status === "failed") renderRequestAgain(b);
  } catch (e) {
    el.innerHTML = `<div class="sheet-head"><p class="bad">${esc(e.message)}</p></div>`;
  }
}

/* 문제 보기 — 지원자가 받는 README 와, 진행자(응시 플랫폼)가 요청·질문에 내주는 자료·답·2단계. 숨은 칸은 두 API 모두 내리지 않는다 */
const inline = (t) =>
  esc(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");

function markdown(src) {
  const out = [];
  const lines = src.replace(/\r/g, "").split("\n");
  for (let i = 0; i < lines.length; ) {
    const l = lines[i];
    if (l.startsWith("```")) {
      const body = [];
      for (i++; i < lines.length && !lines[i].startsWith("```"); i++) body.push(lines[i]);
      i++;
      out.push(`<pre><code>${esc(body.join("\n"))}</code></pre>`);
    } else if (/^#{1,4} /.test(l)) {
      const n = l.match(/^#+/)[0].length;
      out.push(`<h${n}>${inline(l.slice(n + 1))}</h${n}>`);
      i++;
    } else if (/^\s*\|/.test(l)) {
      const rows = [];
      for (; i < lines.length && /^\s*\|/.test(lines[i]); i++) rows.push(lines[i].trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim()));
      const body = rows.filter((r) => !r.every((c) => /^:?-{2,}:?$/.test(c)));
      const [h, ...rest] = body;
      out.push(`<div class="tbl-wrap"><table><thead><tr>${h.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${rest.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
    } else if (/^\s*([-*]|\d+\.) /.test(l)) {
      const ordered = /^\s*\d+\. /.test(l);
      const items = [];
      for (; i < lines.length && /^\s*([-*]|\d+\.) /.test(lines[i]); i++) items.push(`<li>${inline(lines[i].replace(/^\s*([-*]|\d+\.) /, ""))}</li>`);
      out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
    } else if (l.trim()) {
      const para = [];
      for (; i < lines.length && lines[i].trim() && !/^(#{1,4} |```|\s*\||\s*([-*]|\d+\.) )/.test(lines[i]); i++) para.push(inline(lines[i]));
      out.push(`<p>${para.join("<br>")}</p>`);
    } else i++;
  }
  return `<div class="md">${out.join("")}</div>`;
}

function materialsList(m) {
  if (!m.items.length) return `<p class="placeholder">없음</p>`;
  return `<p class="faint">요청 상한 ${m.max_requests ?? "없음"}</p><ul class="bflags">${m.items
    .map((a) => `<li><span class="mono faint">${esc(a.id)}</span> <b>${esc(a.title)}</b><p>${esc(a.content)}</p></li>`)
    .join("")}</ul>`;
}

function facilitatorBlock(f) {
  const s1 = f.stage1;
  const qs = s1.clarifications.items
    .map(
      (c) => `<li><span class="mono faint">${esc(c.id)}</span> <b>${esc(c.topic)}</b>
      <p><span class="faint">이 낱말이 있으면 구체 답:</span> ${esc(c.match_terms.join(", ") || "–")}<br>
      <span class="faint">구체 답:</span> ${esc(c.answer)}${c.answer_hint ? `<br><span class="faint">모호한 질문엔:</span> ${esc(c.answer_hint)}` : ""}</p></li>`,
    )
    .join("");
  const s2 = f.stage2
    ? `<h4>2단계 · ${f.stage2.time_limit_min}분 <span class="faint">(1단계 제출 뒤 공개)</span></h4>
      <div class="md"><p>${esc(f.stage2.scenario).replace(/\n/g, "<br>")}</p></div>
      <div class="eyebrow">제출물</div><ul class="bflags">${f.stage2.deliverables.map((d) => `<li><p>${esc(d)}</p></li>`).join("")}</ul>
      <div class="eyebrow">요청하면 주는 자료</div>${materialsList(f.stage2.materials)}`
    : `<h4>2단계</h4><p class="placeholder">없음</p>`;
  return `<div class="fac"><h4>1단계 · ${s1.time_limit_min}분</h4>
    <div class="eyebrow">요청하면 주는 자료</div>${materialsList(s1.materials)}
    <div class="eyebrow">질문 답 (질문 상한 ${s1.clarifications.max_questions ?? "없음"})</div>
    <p class="faint">${esc(s1.clarifications.rule)}</p><ul class="bflags">${qs || "<li>없음</li>"}</ul>${s2}</div>`;
}

let problemCache = {};
async function openProblem(id, tab) {
  let dlg = $("#problem");
  if (!dlg) {
    dlg = document.createElement("dialog");
    dlg.id = "problem";
    dlg.className = "problem";
    dlg.innerHTML = `<div class="p-head"><h3 id="p-title"></h3>
      <div class="seg" role="group" aria-label="보기" id="p-tabs"><button type="button" data-t="readme">지원자 과제 설명</button><button type="button" data-t="facilitator">진행자 보기</button></div>
      <button type="button" class="btn small" id="p-close">닫기</button></div><div class="p-body" id="p-body"></div>`;
    document.body.append(dlg);
    dlg.addEventListener("click", (ev) => {
      if (ev.target === dlg || ev.target.closest("#p-close")) return dlg.close();
      const t = ev.target.closest("button[data-t]");
      if (t) openProblem(dlg.dataset.id, t.dataset.t);
    });
  }
  dlg.dataset.id = id;
  const b = builds.find((x) => x.build_id === id);
  $("#p-title").textContent = b?.title || id;
  for (const btn of $("#p-tabs").children) btn.setAttribute("aria-pressed", String(btn.dataset.t === tab));
  if (!dlg.open) dlg.showModal();
  const body = $("#p-body");
  const key = `${id}|${tab}`;
  body.innerHTML = `<p class="placeholder">읽는 중…</p>`;
  try {
    problemCache[key] ??= await api(`/builds/${encodeURIComponent(id)}/${tab}`);
    if (dlg.dataset.id !== id) return;
    const v = problemCache[key];
    body.innerHTML = tab === "readme" ? markdown(v.content) : facilitatorBlock(v);
    body.scrollTop = 0;
  } catch (e) {
    body.innerHTML = `<p class="bad">${esc(e.message)}</p>`;
  }
}

$("#list").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-id]");
  if (btn) show(btn.dataset.id);
});
$("#filter").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-f]");
  if (!btn) return;
  filter = btn.dataset.f;
  for (const b of $("#filter").children) b.setAttribute("aria-pressed", String(b === btn));
  renderList();
});
$("#reload").addEventListener("click", () => {
  problemCache = {};
  loadList();
  if (current) show(current);
});
$("#detail").addEventListener("click", async (ev) => {
  if (ev.target.closest("button[data-problem]")) return openProblem(current, "readme");
  const jump = ev.target.closest("a[data-jump]");
  if (jump) {
    ev.preventDefault();
    document.getElementById(jump.dataset.jump)?.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const copy = ev.target.closest("button[data-copy]");
  if (!copy) return;
  try {
    await navigator.clipboard.writeText(copy.dataset.copy);
    copy.textContent = "복사됨";
  } catch {
    copy.textContent = "복사 안 됨";
  }
  setTimeout(() => (copy.textContent = "복사"), 1500);
});

loadList();
