/* 과제 생성 진행 막대 — 빌드의 stage_log(단계·시각·시도·비용)로 그린다. 작업대(app.js)·빌드 화면(builds.js)이 같이 쓴다.
   단계·시간·비용만 — 생성 코드·LLM 출력·숨은 테스트 내용은 넣지 않는다. */
const BUILD_STAGES = [
  ["contract", "계약"],
  ["build", "시작 코드·테스트"],
  ["files", "파일 저장"],
  ["images", "이미지"],
  ["package", "묶음"],
];
const BUILD_RUNNING = ["pending", "generating", "verifying"];

function stageEsc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function stageDur(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}초`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}분 ${s % 60}초` : `${Math.floor(m / 60)}시간 ${m % 60}분`;
}

/* b: GET /builds 의 빌드 한 줄. 진행 기록이 없으면(예전 빌드·적재 빌드) 빈 문자열 */
function stageBar(b, now = Date.now()) {
  const log = (b.stage_log || []).filter((m) => m.attempt === (b.attempt || m.attempt));
  if (!log.length) return b.status === "pending" ? `<div class="stagebar"><span class="st wait">대기열 — builder 가 가져가기를 기다리는 중</span></div>` : "";
  const running = BUILD_RUNNING.includes(b.status);
  const end = b.finished_at ? Date.parse(b.finished_at) : now;
  const seen = new Map(log.map((m, i) => [m.stage, i]));
  const cur = log[log.length - 1].stage;
  const cells = BUILD_STAGES.map(([key, label]) => {
    const i = seen.get(key);
    if (i === undefined) return `<span class="st todo">${label}</span>`;
    const m = log[i];
    const until = i + 1 < log.length ? Date.parse(log[i + 1].at) : end;
    const active = key === cur && running;
    const failed = key === cur && b.status === "failed";
    const cls = failed ? "fail" : active ? "now" : "done";
    const mark = failed ? "✗" : active ? "●" : "✓";
    const cost = m.cost_usd ? ` · $${m.cost_usd.toFixed(3)}` : "";
    const time = active ? `${stageDur(until - Date.parse(m.at))}째` : stageDur(until - Date.parse(m.at));
    return `<span class="st ${cls}" title="${stageEsc(m.at)}">${mark} ${label}<small>${time}${cost}</small></span>`;
  });
  const tail =
    b.status === "failed" ? "" : running ? `<span class="st todo">검토</span>` : `<span class="st done">✓ 검토</span>`;
  const total = (b.started_at ? stageDur(end - Date.parse(b.started_at)) : "") + (b.cost_usd ? ` · $${Number(b.cost_usd).toFixed(3)}` : "");
  const attempt = b.attempt > 1 ? ` · 시도 ${b.attempt}회` : "";
  return `<div class="stagebar">${cells.join('<span class="arrow">→</span>')}<span class="arrow">→</span>${tail}
    <span class="total">${running ? "진행" : "합계"} ${total}${attempt}</span></div>`;
}
