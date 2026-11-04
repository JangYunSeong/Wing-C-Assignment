/* 과제 명세 작업대 — 임시 화면. 기존 API 만 부른다 (새 엔드포인트 없음).
 *   POST /postings/suggest → POST /postings → POST /runs → SSE /runs/{id}/events → GET /runs/{id}
 *   → POST /runs/{id}/approve → GET /runs/{id}/spec
 * API 가 준 글은 모두 esc() 로 넣는다 (LLM 출력에 태그가 섞여도 화면에서 실행되지 않게).
 * 브라우저 저장소에는 실행 id·공고 id·이름(승인자·확인자)만 둔다 — 지원서 내용은 두지 않는다. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const TERMINAL = new Set(["done", "rejected", "failed"]);
const STATUS_KO = { running: "진행 중", awaiting_approval: "승인 대기", done: "완료", rejected: "반려", failed: "실패" };
const SECTION_KO = { duties: "담당업무", required: "자격요건", preferred: "우대사항", other: "기타·제외" };
const ROLE_KO = { key: "핵심", support: "보조", distractor: "방해" };
const REL_KO = { strong: "관계 강함", medium: "관계 중간", weak: "관계 약함" };

/* ── API ─────────────────────────── */
class ApiError extends Error {
  constructor(message, code, status) {
    super(message);
    this.code = code;
    this.status = status;
  }
}
async function api(method, path, { json, form } = {}) {
  const init = { method, headers: { Accept: "application/json" } };
  if (json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  }
  if (form) init.body = form;
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError("서버에 연결하지 못했습니다. 네트워크나 서버 상태를 확인하세요.", "network", 0);
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* 본문 없음 */
  }
  if (!res.ok) {
    const e = data && data.error;
    throw new ApiError((e && e.message) || `요청이 실패했습니다 (HTTP ${res.status})`, e && e.code, res.status);
  }
  return data;
}

/* ── 브라우저 저장 (편의용, 없어도 동작) ── */
const keep = {
  get(k, d) {
    try {
      const v = localStorage.getItem(`taskspec.${k}`);
      return v ? JSON.parse(v) : d;
    } catch {
      return d;
    }
  },
  set(k, v) {
    try {
      localStorage.setItem(`taskspec.${k}`, JSON.stringify(v));
    } catch {
      /* 저장 못 해도 화면은 동작 */
    }
  },
};

function say(el, text, kind = "") {
  el.textContent = text || "";
  el.className = `msg ${kind}`;
}
async function busy(btn, label, fn) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = label;
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}
function seg(ids, panes, onChange) {
  ids.forEach((id, i) =>
    $(`#${id}`).addEventListener("click", () => {
      ids.forEach((b, j) => $(`#${b}`).setAttribute("aria-pressed", String(i === j)));
      panes.forEach((p, j) => ($(`#${p}`).hidden = i !== j));
      if (onChange) onChange(i);
    }),
  );
}
function fmtDur(ms) {
  if (ms == null) return "";
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}초`;
  return `${Math.floor(s / 60)}분 ${Math.round(s % 60)}초`;
}
function ago(iso) {
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (m < 1) return "방금";
  if (m < 60) return `${m}분 전`;
  if (m < 60 * 24) return `${Math.round(m / 60)}시간 전`;
  return `${Math.round(m / 1440)}일 전`;
}

/* ── 공고 이름 ─────────────────────── */
// 온톨로지 원 표기는 "회사 / 직무 (경력)" 처럼 회사가 이미 들어 있다 — 그때는 그대로, 아니면 "회사 · 직무"
function postingName(p) {
  const pos = p.position_name || "";
  if (pos && p.company && pos.includes(p.company)) return pos;
  return [p.company, p.title || pos].filter(Boolean).join(" · ") || p.posting_id || "";
}
function rememberLabel(id, label) {
  if (!id || !label) return;
  const labels = keep.get("labels", {});
  labels[id] = label;
  const ids = Object.keys(labels);
  if (ids.length > 200) delete labels[ids[0]];
  keep.set("labels", labels);
}
const labelOf = (id) => keep.get("labels", {})[id] || id;

/* ── STEP 1 공고 ───────────────────── */
const posting = { mode: 0, bodyMode: 0, suggestion: null, confirmedId: null, picked: null, searched: false };
seg(["pm-text", "pm-id"], ["posting-text-pane", "posting-id-pane"], (i) => {
  posting.mode = i;
  if (i === 1 && !posting.searched) searchPostings();
});

let searchTimer = null;
let searchSeq = 0;
async function searchPostings() {
  posting.searched = true;
  const seq = ++searchSeq;
  const msg = $("#search-msg");
  const params = new URLSearchParams({ q: $("#posting-q").value.trim(), pilot_only: String($("#pilot-only").checked), limit: "20" });
  try {
    const hits = await api("GET", `/postings?${params}`);
    if (seq !== searchSeq) return; // 늦게 온 이전 검색 결과는 버린다
    renderHits(hits);
    say(msg, hits.length ? (hits.length === 20 ? "앞의 20건만 보입니다. 검색어를 더 넣어 좁히세요." : "") : "찾은 공고가 없습니다.");
  } catch (e) {
    if (seq === searchSeq) say(msg, e.message, "err");
  }
}
function renderHits(hits) {
  const ul = $("#posting-hits");
  ul.innerHTML = hits
    .map(
      (h, i) => `<li><button type="button" data-i="${i}" aria-pressed="${posting.picked && posting.picked.id === h.posting_id}">
        <span class="hit-name">${esc(postingName(h))}</span>
        <span class="hit-meta"><span class="src">${h.source === "provisional" ? "임시 공고" : h.in_pilot ? "시범" : "온톨로지"}</span><span>${esc([h.sub, h.industry_id].filter(Boolean).join(" · "))}</span><span class="mono">${esc(h.posting_id)}</span></span>
      </button></li>`,
    )
    .join("");
  ul.querySelectorAll("button[data-i]").forEach((b) =>
    b.addEventListener("click", () => {
      const h = hits[Number(b.dataset.i)];
      pickPosting({ id: h.posting_id, label: postingName(h), meta: [h.sub, h.industry_id].filter(Boolean).join(" · ") });
      ul.querySelectorAll("button[data-i]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    }),
  );
}
function pickPosting(p) {
  posting.picked = p;
  rememberLabel(p.id, p.label);
  const box = $("#posting-picked");
  box.innerHTML = `<span class="eyebrow">고른 공고</span><div class="row"><strong>${esc(p.label)}</strong><button type="button" class="link-btn" id="unpick">다시 고르기</button></div><span class="mono">${esc(p.id)}${p.meta ? ` · ${esc(p.meta)}` : ""}</span>`;
  box.hidden = false;
  $("#unpick").addEventListener("click", () => {
    posting.picked = null;
    box.hidden = true;
    $("#posting-hits").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", "false"));
    $("#posting-q").focus();
  });
}
$("#posting-q").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(searchPostings, 250);
});
$("#pilot-only").addEventListener("change", searchPostings);
seg(["pb-text", "pb-file"], ["p-text-wrap", "p-file-wrap"], (i) => {
  posting.bodyMode = i;
  invalidateSuggestion();
});
$("#confirmed-by").value = keep.get("confirmedBy", "");

function invalidateSuggestion() {
  posting.suggestion = null;
  posting.confirmedId = null;
  $("#cands").hidden = true;
  $("#confirmed").hidden = true;
  say($("#confirm-msg"), "");
}
["#p-company", "#p-title", "#p-body"].forEach((s) => $(s).addEventListener("input", invalidateSuggestion));
$("#p-file").addEventListener("change", (ev) => {
  const f = ev.target.files[0];
  $("#p-file-label").textContent = f ? f.name : "공고 파일 고르기";
  invalidateSuggestion();
});

$("#suggest-btn").addEventListener("click", async () => {
  const msg = $("#suggest-msg");
  const fd = new FormData();
  fd.append("company", $("#p-company").value.trim());
  fd.append("title", $("#p-title").value.trim());
  if (posting.bodyMode === 1) {
    const f = $("#p-file").files[0];
    if (!f) return say(msg, "공고 파일을 고르세요.", "err");
    fd.append("file", f, f.name);
  } else {
    const t = $("#p-body").value.trim();
    if (!t) return say(msg, "공고 원문을 붙여 넣으세요.", "err");
    fd.append("text", t);
  }
  say(msg, "문장을 나누고 직군을 추천하는 중입니다. LLM 을 쓰면 30초 안팎 걸립니다.");
  try {
    const s = await busy($("#suggest-btn"), "나누는 중…", () => api("POST", "/postings/suggest", { form: fd }));
    posting.suggestion = s;
    posting.confirmedId = null;
    renderSuggestion(s);
    say(msg, "");
  } catch (e) {
    say(msg, e.message, "err");
  }
});

function renderSuggestion(s) {
  const counts = { duties: 0, required: 0, preferred: 0, other: 0 };
  s.lines.forEach((l) => (counts[l.section] = (counts[l.section] || 0) + 1));
  $("#lines-head").textContent = `나눈 문장 · ${s.lines.length}개 · ${s.segment_method}${s.llm_used ? " · LLM 사용" : ""}`;
  $("#sec-counts").innerHTML = Object.entries(SECTION_KO)
    .map(([k, ko]) => `<span class="${k === "other" ? "other" : ""}"><b class="num">${counts[k] || 0}</b> ${ko}</span>`)
    .join("");
  $("#lines-list").innerHTML = s.lines
    .map((l) => `<li><span>${esc(SECTION_KO[l.section] || l.section)}</span>${esc(l.text)}</li>`)
    .join("");
  $("#sub-list").innerHTML = s.sub_candidates.length
    ? s.sub_candidates
        .slice(0, 5)
        .map(
          (c, i) => `<label class="cand" title="${esc((c.reasons || []).join(" · "))}">
            <input type="radio" name="sub" value="${esc(c.id)}" ${i === 0 ? "checked" : ""}>
            <span>${esc(c.label)} <span class="mono">${esc(c.id)}</span></span><span class="score num">${Number(c.score).toFixed(2)}</span></label>`,
        )
        .join("")
    : `<p class="msg err">직군 후보가 없습니다. 공고 원문을 확인하세요.</p>`;
  $("#industry").innerHTML = s.industry_candidates
    .map((c) => `<option value="${esc(c.id)}">${esc(c.label)} (${esc(c.id)}) · ${Number(c.score).toFixed(2)}</option>`)
    .join("");
  $("#cands").hidden = false;
  $("#confirmed").hidden = true;
}

$("#confirm-btn").addEventListener("click", async () => {
  const msg = $("#confirm-msg");
  const s = posting.suggestion;
  const sub = document.querySelector('input[name="sub"]:checked');
  const by = $("#confirmed-by").value.trim();
  if (!s || !sub) return say(msg, "먼저 직군을 추천받으세요.", "err");
  if (!$("#industry").value) return say(msg, "업종을 고르세요.", "err");
  if (!by) return say(msg, "확인자 이름을 적으세요.", "err");
  keep.set("confirmedBy", by);
  try {
    const r = await busy($("#confirm-btn"), "확정하는 중…", () =>
      api("POST", "/postings", {
        json: { suggestion_id: s.suggestion_id, sub: sub.value, industry_id: $("#industry").value, confirmed_by: by },
      }),
    );
    posting.confirmedId = r.posting_id;
    rememberLabel(r.posting_id, [$("#p-company").value.trim(), $("#p-title").value.trim()].filter(Boolean).join(" · "));
    const box = $("#confirmed");
    box.innerHTML = `<span class="mono">${esc(r.posting_id)}</span><span>임시 공고 · 역량 <b class="num">${r.competencies}</b>개 · 매칭 안 된 문장 <b class="num">${r.unmatched_lines}</b>개</span>`;
    box.hidden = false;
    say(msg, "");
  } catch (e) {
    say(msg, e.message, "err");
  }
});


/* ── STEP 2 이력서 ─────────────────── */
const resume = { mode: 0, file: null };
seg(["rm-file", "rm-text"], ["resume-file-pane", "resume-text-pane"], (i) => (resume.mode = i));
const drop = $("#drop");
const fileInput = $("#resume-file");
function setResumeFile(f) {
  resume.file = f || null;
  $("#file-row").hidden = !f;
  if (f) {
    $("#file-name").textContent = `${f.name} · ${Math.max(1, Math.round(f.size / 1024))}KB`;
    $("#file-ext").textContent = (f.name.split(".").pop() || "").toUpperCase();
  }
}
fileInput.addEventListener("change", () => setResumeFile(fileInput.files[0]));
["dragenter", "dragover"].forEach((t) =>
  drop.addEventListener(t, (ev) => {
    ev.preventDefault();
    drop.classList.add("over");
  }),
);
["dragleave", "drop"].forEach((t) =>
  drop.addEventListener(t, (ev) => {
    ev.preventDefault();
    drop.classList.remove("over");
  }),
);
drop.addEventListener("drop", (ev) => ev.dataTransfer.files[0] && setResumeFile(ev.dataTransfer.files[0]));
$("#file-clear").addEventListener("click", () => {
  fileInput.value = "";
  setResumeFile(null);
});

$("#run-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const msg = $("#run-msg");
  const pid = posting.mode === 0 ? posting.confirmedId : posting.picked && posting.picked.id;
  if (!pid) {
    return say(msg, posting.mode === 0 ? "STEP 1 에서 공고를 확정하세요." : "STEP 1 에서 공고를 찾아 고르세요.", "err");
  }
  let file = null;
  if (resume.mode === 0) file = resume.file;
  else {
    const t = $("#resume-text").value.trim();
    if (t) file = new File([t], "resume.txt", { type: "text/plain" });
  }
  if (!file) return say(msg, "이력서 파일이나 텍스트를 넣으세요.", "err");
  const fd = new FormData();
  fd.append("posting_id", pid);
  fd.append("resumes", file, file.name);
  try {
    const r = await busy($("#run-btn"), "보내는 중…", () => api("POST", "/runs", { form: fd }));
    rememberRun(r.run_id, pid);
    say(msg, "실행을 시작했습니다. 처음 보는 공고면 공통 과제를 만드느라 3~5분 걸립니다.", "ok");
    openRun(r.run_id, { live: true });
  } catch (e) {
    say(msg, e.message, "err");
  }
});

/* ── 최근 실행 ─────────────────────── */
function rememberRun(runId, postingId) {
  const list = [{ run_id: runId, posting_id: postingId, at: new Date().toISOString() }, ...keep.get("runs", []).filter((r) => r.run_id !== runId)].slice(0, 10);
  keep.set("runs", list);
  renderRecent();
}
function renderRecentNames() {
  const runs = new Map(keep.get("runs", []).map((r) => [r.run_id, r.posting_id]));
  $("#recent")
    .querySelectorAll("button[data-run]")
    .forEach((b) => {
      const el = b.querySelector(".recent-name");
      if (el && runs.has(b.dataset.run)) el.textContent = labelOf(runs.get(b.dataset.run));
    });
}
async function renderRecent() {
  const list = keep.get("runs", []);
  const ul = $("#recent");
  if (!list.length) {
    ul.innerHTML = `<li class="recent-empty">아직 없습니다.</li>`;
    return;
  }
  ul.innerHTML = list
    .map(
      (r) => `<li><button type="button" data-run="${esc(r.run_id)}">
        <span><span class="recent-name">${esc(labelOf(r.posting_id))}</span><span class="sub">${esc(ago(r.at))} · 실행 <span class="mono">${esc(r.run_id.slice(0, 8))}</span></span></span>
        <span class="pill" data-pill="${esc(r.run_id)}">…</span></button></li>`,
    )
    .join("");
  ul.querySelectorAll("button[data-run]").forEach((b) => b.addEventListener("click", () => openRun(b.dataset.run, { live: false })));
  await Promise.all(
    list.map(async (r) => {
      const pill = ul.querySelector(`[data-pill="${CSS.escape(r.run_id)}"]`);
      try {
        const v = await api("GET", `/runs/${encodeURIComponent(r.run_id)}`);
        pill.className = `pill ${v.status}`;
        pill.textContent = STATUS_KO[v.status] || v.status;
      } catch {
        pill.className = "pill";
        pill.textContent = "없음";
      }
    }),
  );
}

/* ── 실행 보기 ─────────────────────── */
const PHASES = [
  { no: "①", name: "공고 프로필", steps: [["posting_profile", "공고 역량 조회"]] },
  { no: "②", name: "입력·가명화·주장", steps: [["ingest", "문서 읽기"], ["pseudonymize", "가명화"], "boundary", ["extract_claims", "주장 뽑기"], ["assign_roles", "역할 판정"]] },
  { no: "③", name: "근거", steps: [["retrieve_t1", "역량 카드 찾기"], ["evidence", "NCS·ESCO 근거"]] },
  { no: "④", name: "대조·명세·승인", steps: [["compare", "대조"], ["draft_spec", "명세 초안"], ["approval", "승인"], ["finalize", "확정"]] },
];
const ALIAS = { retrieve_t2: "retrieve_t1" };
const ORDER = PHASES.flatMap((p) => p.steps.filter((x) => x !== "boundary").map((x) => x[0]));
const run = { id: null, es: null, view: null, spec: null, live: false, started: null, node: null, nodeAt: null, dur: {}, lastKey: null, timer: null, posting: null };

async function openRun(id, { live }) {
  if (run.es) run.es.close();
  clearInterval(run.timer);
  Object.assign(run, { id, es: null, view: null, spec: null, live, started: live ? Date.now() : null, node: null, nodeAt: null, dur: {}, lastKey: null, posting: null });
  try {
    history.replaceState(null, "", `?run=${encodeURIComponent(id)}`);
  } catch {
    /* 주소 갱신 실패는 무시 */
  }
  $("#sheet-empty").hidden = true;
  $("#sheet-run").hidden = false;
  $("#run-id").textContent = id.slice(0, 8);
  $("#run-title").textContent = "불러오는 중…";
  ["#summary", "#compare", "#common", "#slots", "#verify", "#decide"].forEach((s) => ($(s).innerHTML = ""));
  try {
    const view = await api("GET", `/runs/${encodeURIComponent(id)}`);
    await applyView(view);
    if (!TERMINAL.has(view.status)) watch(id);
  } catch (e) {
    $("#run-title").textContent = "실행을 찾지 못했습니다";
    $("#run-meta").textContent = e.message;
  }
}

function watch(id) {
  const es = new EventSource(`/runs/${encodeURIComponent(id)}/events`);
  run.es = es;
  es.addEventListener("status", (e) => onStatus(JSON.parse(e.data)));
  if (run.live) run.timer = setInterval(() => renderProgressAside(), 1000);
}

function trackNode(node, status) {
  const key = ALIAS[node] || node;
  const now = Date.now();
  if (key !== run.node) {
    if (run.node && run.nodeAt && run.live) run.dur[run.node] = (run.dur[run.node] || 0) + (now - run.nodeAt);
    run.node = key;
    run.nodeAt = now;
  }
  if (TERMINAL.has(status)) run.node = key;
}

async function onStatus(ev) {
  if (ev.run_id !== run.id) return;
  trackNode(ev.current_node, ev.status);
  setStatus(ev.status);
  renderPipe(ev.current_node, ev.status);
  const key = `${ev.status}|${ev.revision}`;
  const changed = key !== run.lastKey;
  run.lastKey = key;
  if (TERMINAL.has(ev.status)) {
    if (run.es) run.es.close();
    run.es = null;
    clearInterval(run.timer);
  }
  if (changed && (ev.status === "awaiting_approval" || TERMINAL.has(ev.status))) {
    try {
      await applyView(await api("GET", `/runs/${encodeURIComponent(run.id)}`));
    } catch (e) {
      $("#run-meta").textContent = e.message;
    }
  }
}

function setStatus(status) {
  const pill = $("#run-status");
  pill.className = `pill ${status}`;
  pill.textContent = STATUS_KO[status] || status;
}

function renderPipe(current, status) {
  const cur = current
    ? ORDER.indexOf(ALIAS[current] || current)
    : status === "done"
      ? ORDER.length
      : status === "rejected"
        ? ORDER.indexOf("approval")
        : -1;
  $("#pipe").innerHTML = PHASES.map(
    (p) => `<div class="phase"><div class="phase-name"><b>${p.no}</b>${p.name}</div>${p.steps
      .map((x) => {
        if (x === "boundary") return `<div class="boundary">여기부터 외부 LLM</div>`;
        const i = ORDER.indexOf(x[0]);
        let st = "wait";
        if (status === "done" || (cur >= 0 && i < cur)) st = "done";
        else if (i === cur) st = status === "failed" ? "failed" : status === "rejected" ? "done" : "active";
        let t = run.dur[x[0]] ? fmtDur(run.dur[x[0]]) : "";
        if (st === "active") t = x[0] === "approval" && status === "awaiting_approval" ? "대기" : "진행 중";
        if (st === "failed") t = "멈춤";
        return `<div class="node ${st}"><span class="dot"></span><span class="lbl">${x[1]}<span class="mono">${x[0]}</span></span><span class="t num">${t}</span></div>`;
      })
      .join("")}</div>`,
  ).join("");
  renderProgressAside(cur, status);
}

function renderProgressAside(cur, status) {
  const v = run.view;
  status = status || (v && v.status);
  if (cur === undefined) cur = run.node ? ORDER.indexOf(run.node) : -1;
  const doneN = status === "done" ? ORDER.length : Math.max(0, cur);
  let text = `${ORDER.length}단계 중 ${doneN}단계 끝남`;
  if (run.started) text += ` · 경과 ${fmtDur(Date.now() - run.started)}`;
  $("#progress-aside").textContent = text;
}

async function applyView(view) {
  run.view = view;
  setStatus(view.status);
  if (!run.node) run.node = view.current_node ? ALIAS[view.current_node] || view.current_node : null;
  renderPipe(view.current_node, view.status);
  if (view.ontology_version) {
    $("#meta-onto").textContent = view.ontology_version;
    $("#meta-onto").hidden = false;
  }
  let spec = view.spec;
  if (view.status === "done") {
    try {
      spec = await api("GET", `/runs/${encodeURIComponent(view.run_id)}/spec`);
    } catch (e) {
      spec = null;
    }
  }
  run.spec = spec || run.spec;
  const s = run.spec;
  $("#run-rev").textContent = view.revision ? `${view.revision}판` : "초안 전";
  if (s) {
    loadPostingLabel(s.posting_id);
    const prompts = [...new Set((s.common_refs || []).map((r) => r.prompt_ref))].join(" · ");
    $("#meta-prompts").textContent = prompts;
    $("#meta-prompts").hidden = !prompts;
  } else {
    $("#run-title").textContent = view.status === "running" ? "명세를 만드는 중입니다" : "명세가 없습니다";
    $("#run-meta").textContent = "";
  }
  renderSummary(view, s);
  renderCompare(view.approval_summary);
  renderCommon(s);
  renderSlots(s);
  renderVerify(s);
  renderDecide(view, s);
}

async function loadPostingLabel(pid) {
  const total = run.view && run.view.approval_summary ? run.view.approval_summary.total_time_min : null;
  const meta = [`<span class="mono">${esc(pid)}</span>`];
  if (total) meta.push(`<span>응시 시간 합계 <b class="num">${total}분</b></span>`);
  $("#run-title").textContent = labelOf(pid);
  $("#run-meta").innerHTML = meta.join("");
  try {
    const p = await api("GET", `/postings/${encodeURIComponent(pid)}`);
    const name = postingName(p);
    if (name) {
      $("#run-title").textContent = name;
      rememberLabel(pid, name);
      renderRecentNames();
    }
    const sub = [p.source === "provisional" ? "임시 공고" : p.in_pilot ? "시범 공고" : "온톨로지 공고", p.sub, p.industry_id].filter(Boolean).join(" · ");
    $("#run-meta").insertAdjacentHTML("beforeend", `<span>${esc(sub)}</span>`);
  } catch {
    /* 공고 정보를 못 가져와도 id 로 보여준다 */
  }
}

/* 요약 */
function collectFlags(view, s) {
  const out = [];
  const sum = view.approval_summary;
  (s ? s.review_flags : (sum && sum.review_flags) || []).forEach((f) => out.push(["명세", f]));
  (s ? s.common_tasks : []).forEach((t) => {
    (t.review_flags || []).forEach((f) => out.push(["공통 과제", f]));
    (t.slots || []).forEach((sl) => (sl.review_flags || []).forEach((f) => out.push([sl.task_id, f])));
  });
  (s ? s.slot_assignments : []).forEach((a) => (a.review_flags || []).forEach((f) => out.push([a.claim_id.slice(0, 10), f])));
  return out;
}
function renderSummary(view, s) {
  const el = $("#summary");
  const flags = collectFlags(view, s);
  const errors = view.errors || [];
  const common = s ? s.common_tasks : [];
  const slots = common.flatMap((t) => t.slots || []);
  const assigns = s ? s.slot_assignments : [];
  const commonMin = common.reduce((a, t) => a + (t.time_limit_min || 0), 0);
  const fact = (v, unit, k, attn) => `<div class="fact ${attn ? "attn" : ""}"><span class="v num">${v}<small>${unit}</small></span><span class="k">${k}</span></div>`;
  // 재요청 기록(draft_invalid 등)은 정상 흐름 — 실패만 검토 표시와 함께 세고, 나머지는 접어 둔다
  const failed = errors.filter((e) => !/invalid$/.test(e.code));
  const notes = errors.filter((e) => /invalid$/.test(e.code));
  const errLi = (e) =>
    `<li class="error"><span class="where">${esc(e.node)}</span><span>${esc(e.code)}${e.message ? ` — ${esc(e.message)}` : ""}${e.ref_id ? ` (${esc(e.ref_id)})` : ""}</span></li>`;
  const attn = flags.length + failed.length;
  el.innerHTML = `
    <div class="facts">
      ${fact(common.length, "개", `공통 과제${commonMin ? ` · ${commonMin}분` : ""}`)}
      ${fact(slots.length, "개", "확장 슬롯")}
      ${fact(assigns.length, "건", "검증할 주장")}
      ${fact(attn, "건", "검토 표시", attn > 0)}
    </div>
    ${
      attn
        ? `<ul class="flags" style="margin-top:12px">
            ${failed.map(errLi).join("")}
            ${flags.map(([w, f]) => `<li><span class="where">${esc(w)}</span><span>${esc(f)}</span></li>`).join("")}
          </ul>`
        : s
          ? `<p class="placeholder" style="margin-top:12px">검토 표시가 없습니다.</p>`
          : ""
    }
    ${
      notes.length
        ? `<details class="lines" style="margin-top:10px"><summary>실행 기록 ${notes.length}건 (자동 검사 후 재요청 — 최종 결과는 위 검토 표시에 반영)</summary><ul>${notes
            .map((e) => `<li><span>${esc(e.node)}</span>${esc(e.message || e.code)}</li>`)
            .join("")}</ul></details>`
        : ""
    }`;
}

/* 대조 */
function renderCompare(sum) {
  const el = $("#compare");
  const c = sum && sum.comparison;
  if (!c) {
    el.innerHTML = `<p class="placeholder">승인 대기가 되면 대조 결과가 나옵니다.</p>`;
    return;
  }
  const chips = (xs) => (xs.length ? `<div class="chips">${xs.join("")}</div>` : `<span class="placeholder">없음</span>`);
  el.innerHTML = `
    <div class="cmp">
      <div><h4>겹침</h4>${chips(c.overlap.map((x) => `<span class="chip">${esc(x)}</span>`))}</div>
      <div><h4>공고에만</h4>${chips(c.posting_only.map((x) => `<span class="chip">${esc(x)}</span>`))}</div>
      <div><h4>지원자에게만</h4>${chips(
        c.applicant_only.map((x) => `<span class="chip ${x.relatedness === "weak" ? "weak" : ""}">${esc(x.label)}${x.relatedness ? `<span class="lv">${REL_KO[x.relatedness] || ""}</span>` : ""}</span>`),
      )}</div>
    </div>
    ${c.uncovered_claims ? `<p class="placeholder" style="margin-top:10px">역량에 맞지 않은 주장 ${c.uncovered_claims}건</p>` : ""}
    ${
      c.claims_to_verify.length
        ? `<div class="tbl-wrap" style="margin-top:12px"><table><thead><tr><th>주장</th><th>점수</th><th>왜 검증하나</th></tr></thead><tbody>
          ${c.claims_to_verify.map((v) => `<tr><td class="mono">${esc(v.claim_id)}</td><td class="num">${Number(v.score).toFixed(1)}</td><td>${esc(v.reasons.join(" · "))}</td></tr>`).join("")}
        </tbody></table></div>`
        : ""
    }`;
}

/* 공통 과제 */
function compChips(list) {
  return `<div class="chips">${(list || []).map((c) => `<span class="chip">${esc(c.label)}${c.level ? `<span class="lv">${esc(c.level)}</span>` : ""}</span>`).join("")}</div>`;
}
function renderTask(t) {
  const arts = t.artifacts || [];
  const a = t.behavioral_anchors;
  const anchor = (lv, x) =>
    x
      ? `<div class="anchor l${lv}"><span class="lvl"><i>${lv}</i>수준 ${lv}</span><p>${esc(x.behavior)}</p><div class="crit">${(x.criterion_ids || []).map((c) => `<span>${esc(c)}</span>`).join("")}</div></div>`
      : "";
  return `<div class="task">
    <h3 class="task-title">${esc(t.title || "(서술 없음 — 결정적 골격만)")}</h3>
    <div class="task-meta">
      <span><b>${t.time_limit_min}분</b></span><span>난이도 <b>${esc(t.difficulty_target)}</b></span>
      ${t.tech_env && t.tech_env.languages.length ? `<span>기술 <b>${esc(t.tech_env.languages.join(", "))}</b></span>` : ""}
      ${arts.length ? `<span>자료 <b>${arts.length}개</b>${t.max_requests ? ` 중 <b>${t.max_requests}개</b>까지 요청` : ""}</span>` : ""}
      ${t.source_incident ? `<span class="mono">${esc(t.source_incident)}</span>` : ""}
    </div>
    ${compChips(t.target_competencies)}
    ${t.scenario ? `<p class="scenario">${esc(t.scenario)}</p>` : ""}
    ${t.deliverables && t.deliverables.length ? `<div><span class="eyebrow">제출물</span><ol class="deliv">${t.deliverables.map((d) => `<li>${esc(d)}</li>`).join("")}</ol></div>` : ""}
    ${
      arts.length || (t.traps || []).length
        ? `<div class="split">
        <div><div class="block-head"><span class="eyebrow">자료</span><span class="only-approver">역할·본문은 승인자에게만 보임</span></div>
          ${
            arts.length
              ? `<div class="arts">${arts.map((x) => `<details><summary><span class="mono">${esc(x.artifact_id)}</span><span class="role ${esc(x.role)}">${ROLE_KO[x.role] || esc(x.role)}</span><span>${esc(x.title)}</span><span class="caret" aria-hidden="true">▶</span></summary><pre>${esc(x.content)}</pre></details>`).join("")}</div>`
              : `<span class="placeholder">자료 없음 (상황에 다 들어 있음)</span>`
          }</div>
        <div><div class="block-head"><span class="eyebrow">함정</span><span class="only-approver">승인자에게만 보임</span></div>
          <ul class="traps">${(t.traps || []).map((x) => `<li><span class="tid">${esc(x.trap_id)}</span><span>${esc(x.description)}<span class="why">${esc(x.why_ai_fails)}</span></span></li>`).join("")}</ul></div>
      </div>`
        : ""
    }
    ${a ? `<div><div class="block-head" style="margin-bottom:8px"><span class="eyebrow">행동 기준</span><span class="aside">채점자는 결과물이 아니라 과정 기록을 봅니다</span></div><div class="anchors">${anchor(1, a.level_1)}${anchor(3, a.level_3)}${anchor(5, a.level_5)}</div></div>` : ""}
  </div>`;
}
function renderCommon(s) {
  const el = $("#common");
  if (!s) {
    el.innerHTML = `<p class="placeholder">명세 초안이 나오면 보입니다.</p>`;
    return;
  }
  el.innerHTML = s.common_tasks.length ? s.common_tasks.map(renderTask).join("") : `<p class="placeholder">공통 과제가 없습니다.</p>`;
}

/* 확장 슬롯 */
function renderSlots(s) {
  const el = $("#slots");
  const slots = s ? s.common_tasks.flatMap((t) => t.slots || []) : [];
  if (!slots.length) {
    el.innerHTML = `<p class="placeholder">${s ? "확장 슬롯이 없습니다." : "명세 초안이 나오면 보입니다."}</p>`;
    return;
  }
  const who = {};
  (s.slot_assignments || []).forEach((a) => a.slot_id && (who[a.slot_id] = a.claim_id));
  el.innerHTML = `<div class="slots">${slots
    .map(
      (sl) => `<div class="slot ${who[sl.task_id] ? "taken" : ""}">
        <div class="slot-top"><span class="slot-id">${esc(sl.task_id)}</span><h4>${esc(sl.title || "(서술 없음)")}</h4></div>
        <span class="sm">${esc(sl.target_competencies.map((c) => `${c.label}${c.level ? ` · ${c.level}` : ""}`).join(", "))} · ${sl.time_limit_min}분 · 함정 ${(sl.traps || []).length}${(sl.artifacts || []).length ? ` · 자료 ${sl.artifacts.length}개 중 ${sl.max_requests || "?"}개 요청` : ""}</span>
        ${who[sl.task_id] ? `<span class="who">이 지원자 배정: ${esc(who[sl.task_id])}</span>` : ""}
        ${sl.scenario ? `<details><summary>상황 보기</summary><p>${esc(sl.scenario)}</p></details>` : ""}
      </div>`,
    )
    .join("")}</div>`;
}

/* 경험 검증 */
function renderVerify(s) {
  const el = $("#verify");
  const list = s ? s.slot_assignments || [] : [];
  if (!list.length) {
    el.innerHTML = `<p class="placeholder">${s ? "검증할 주장이 없습니다." : "명세 초안이 나오면 보입니다."}</p>`;
    return;
  }
  el.innerHTML = `<div class="claims">${list
    .map(
      (a) => `<div class="claim">
        <div class="claim-side">
          <span class="cid">${esc(a.claim_id)}</span>
          <span>${a.slot_id ? `<span class="pill awaiting_approval">슬롯 ${esc(a.slot_id)}</span>` : `<span class="pill">슬롯 없음</span>`}</span>
          <span class="basis">${esc(a.basis)}</span>
        </div>
        <div class="claim-main">
          ${a.link_text ? `<span class="link-text"><small>연결 문구 · 지원자에게 보임</small>${esc(a.link_text)}</span>` : ""}
          ${
            a.follow_up_questions.length
              ? `<div><span class="eyebrow">면접 확인 질문 · 면접관용</span><ol class="qs">${a.follow_up_questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ol></div>`
              : `<span class="placeholder">면접 질문 없음</span>`
          }
          ${(a.review_flags || []).map((f) => `<span class="claim-flag">${esc(f)}</span>`).join("")}
        </div>
      </div>`,
    )
    .join("")}</div>`;
}

/* 승인 */
function renderDecide(view, s) {
  const el = $("#decide");
  if (view.status === "done" && s && s.approval) {
    const ap = s.approval;
    el.innerHTML = `<div class="banner ok"><strong>승인되어 확정되었습니다</strong>
      <span>${esc(ap.reviewer)} · ${esc(new Date(ap.at).toLocaleString("ko-KR"))}${ap.comment ? ` · ${esc(ap.comment)}` : ""}</span></div>
      <div class="actions" style="margin-top:12px"><button type="button" class="btn small" id="dl-spec">명세 JSON 내려받기</button></div>`;
    $("#dl-spec").addEventListener("click", () => {
      const blob = new Blob([JSON.stringify(s, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${s.spec_id || "task_spec"}.json`;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => {
        URL.revokeObjectURL(a.href);
        a.remove();
      }, 0);
    });
    return;
  }
  if (view.status === "rejected") {
    el.innerHTML = `<div class="banner risk"><strong>반려되었습니다</strong><span>이 실행은 더 진행하지 않습니다.</span></div>`;
    return;
  }
  if (view.status === "failed") {
    el.innerHTML = `<div class="banner risk"><strong>실행이 실패했습니다</strong><span>요약의 오류 목록과 서버 로그(실행 id ${esc(view.run_id)})를 확인하세요.</span></div>`;
    return;
  }
  if (view.status !== "awaiting_approval" || !s) {
    el.innerHTML = `<div class="banner info"><strong>아직 승인할 단계가 아닙니다</strong><span>명세 초안이 나오면 여기서 승인하거나 수정을 요청합니다.</span></div>`;
    return;
  }
  const slots = s.common_tasks.flatMap((t) => t.slots || []);
  const assigns = s.slot_assignments || [];
  el.innerHTML = `
    ${view.approval_error ? `<div class="banner risk" style="margin-bottom:12px"><strong>직전 결정이 거부되었습니다</strong><span>${esc(view.approval_error)}</span></div>` : ""}
    <form class="decide" id="decide-form" autocomplete="off">
      <div class="choice full" role="radiogroup" aria-label="결정">
        <label for="d-approve"><input type="radio" name="decision" id="d-approve" value="approved" checked>승인<span>명세를 확정합니다. 승인 전 공통 과제·슬롯도 함께 승인됩니다.</span></label>
        <label for="d-revise"><input type="radio" name="decision" id="d-revise" value="revise">수정 요청<span>고칠 점을 적으면 다시 만듭니다.</span></label>
        <label for="d-reject" class="reject"><input type="radio" name="decision" id="d-reject" value="rejected">반려<span>이 지원자 명세를 만들지 않습니다.</span></label>
      </div>
      <label class="field" for="reviewer">승인자<input type="text" id="reviewer" maxlength="200" value="${esc(keep.get("reviewer", ""))}"></label>
      <label class="field" for="revise-target" id="target-wrap" hidden>무엇을 다시 만들까
        <select id="revise-target">
          <option value="verification">경험 검증만 (공통 과제 유지)</option>
          <option value="common">공통 과제·슬롯 (공고 단위 새 판)</option>
          <option value="all">전부</option>
        </select>
      </label>
      <label class="field full" for="comment" id="comment-wrap">메모
        <textarea id="comment" rows="3" maxlength="4000" placeholder="승인 메모 (선택)"></textarea>
      </label>
      ${
        assigns.length
          ? `<div class="full reassign" id="reassign" hidden><span class="eyebrow">슬롯 배정 바꾸기 (선택)</span>
          ${assigns
            .map(
              (a, i) => `<div class="r"><span class="mono" title="${esc(a.claim_id)}">${esc(a.claim_id)}</span><select data-claim="${esc(a.claim_id)}" id="ra-${i}">
                <option value="">규칙대로 (${a.slot_id ? esc(a.slot_id) : "슬롯 없음"})</option>
                ${slots.map((sl) => `<option value="${esc(sl.task_id)}">${esc(sl.task_id)} · ${esc((sl.title || "").slice(0, 28))}</option>`).join("")}
                <option value="__none__">슬롯 없이 면접 질문만</option></select></div>`,
            )
            .join("")}</div>`
          : ""
      }
      <div class="full actions"><button type="submit" class="btn primary" id="decide-btn">승인하기</button><span class="msg" id="decide-msg" role="status"></span></div>
    </form>`;
  const form = $("#decide-form");
  const sync = () => {
    const v = form.querySelector('input[name="decision"]:checked').value;
    $("#target-wrap").hidden = v !== "revise";
    if ($("#reassign")) $("#reassign").hidden = v !== "revise";
    $("#comment-wrap").firstChild.textContent = v === "revise" ? "고칠 점 (필수)" : "메모";
    $("#comment").placeholder = v === "revise" ? "예: 함정 t1 은 근거 없는 증설로 한정해 주세요." : v === "rejected" ? "반려 사유 (선택)" : "승인 메모 (선택)";
    $("#decide-btn").textContent = { approved: "승인하기", revise: "수정 요청 보내기", rejected: "반려하기" }[v];
    say($("#decide-msg"), "");
  };
  form.addEventListener("change", sync);
  sync();
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = $("#decide-msg");
    const status = form.querySelector('input[name="decision"]:checked').value;
    const reviewer = $("#reviewer").value.trim();
    const comment = $("#comment").value.trim();
    if (!reviewer) return say(msg, "승인자 이름을 적으세요.", "err");
    if (status === "revise" && !comment) {
      $("#comment").focus();
      return say(msg, "수정 요청에는 고칠 점이 필요합니다.", "err");
    }
    keep.set("reviewer", reviewer);
    const body = { status, reviewer, comment: comment || null, at: new Date().toISOString() };
    if (status === "revise") {
      body.revise_target = $("#revise-target").value;
      const reassign = {};
      form.querySelectorAll("select[data-claim]").forEach((sel) => {
        if (sel.value) reassign[sel.dataset.claim] = sel.value === "__none__" ? null : sel.value;
      });
      body.reassign = reassign;
    }
    try {
      await busy($("#decide-btn"), "보내는 중…", () => api("POST", `/runs/${encodeURIComponent(run.id)}/approve`, { json: body }));
      say(msg, status === "revise" ? "수정 요청을 보냈습니다. 다시 만드는 중입니다." : "보냈습니다. 확정하는 중입니다.", "ok");
      setStatus("running");
      run.lastKey = null;
      if (!run.es) watch(run.id);
    } catch (e) {
      say(msg, e.message, "err");
    }
  });
}

/* ── 시작 ─────────────────────────── */
renderRecent();
const initial = new URLSearchParams(location.search).get("run");
if (initial) openRun(initial, { live: false });
