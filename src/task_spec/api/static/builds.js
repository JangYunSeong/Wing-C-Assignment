/* 공통 과제 빌드 — 임시 화면 (api/routers/ui.py 가 /ui/builds.html 로 내준다).
   기존 API(/builds · /builds/{id} · /artifacts · /deliveries · /delivery-targets)만 부른다.
   API 는 계약·숨은 계획·테스트 본문을 내주지 않으므로 이 화면에도 없다. */
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
        ${pill(b.status)}</button></li>`,
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
    const [b, arts, ds, targets] = await Promise.all([
      api(`/builds/${enc}`),
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
        ${b.approved_by ? `<span>승인 ${esc(b.approved_by)} · ${when(b.approved_at)}</span>` : ""}</div></div>
        <div class="right">${pill(b.status)}</div></div>
      <nav class="jump" aria-label="빌드 안에서 이동"><a href="#" data-jump="b-verify">검증</a><a href="#" data-jump="b-flags">검토 표시</a><a href="#" data-jump="b-arts">이미지 묶음</a><a href="#" data-jump="b-deliv">전달</a></nav>
      <section class="block" id="b-verify"><div class="block-head"><h3>검증 결과</h3><span class="aside">채점용 이미지로 채점한 구현별 통과 수</span></div>
        ${verificationTable(b.verification)}
        <div class="kv"><span>오버레이 파일</span><b>${esc(files)}</b><span>수리</span><b>${b.repairs}건</b>
        <span>시작 코드 해시</span><b class="mono">${esc((b.tree_sha256 || "–").slice(0, 16))}</b><span>채점 내용 해시</span><b class="mono">${esc((b.grader_sha256 || "–").slice(0, 16))}</b>
        <span>온톨로지</span><b class="mono">${esc(b.ontology_version)}</b></div></section>
      <section class="block" id="b-flags"><div class="block-head"><h3>검토 표시</h3><span class="aside">승인 전에 표시마다 승인자 판단이 필요합니다 · ${decided}/${b.review_flags.length} 판단</span></div>${flagsBlock(b.review_flags)}</section>
      <section class="block" id="b-arts"><div class="block-head"><h3>이미지 묶음</h3><span class="aside">받는 곳별 폴더 — 응시용은 응시 환경, 채점용은 채점 서버로만</span></div>${artifactsBlock(arts)}</section>
      <section class="block" id="b-deliv"><div class="block-head"><h3>전달</h3></div>${deliveriesBlock(ds, targets)}</section>`;
  } catch (e) {
    el.innerHTML = `<div class="sheet-head"><p class="bad">${esc(e.message)}</p></div>`;
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
  loadList();
  if (current) show(current);
});
$("#detail").addEventListener("click", async (ev) => {
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
