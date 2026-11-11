// 채점 하네스 — 서버 API 를 가로채 응답 시점을 테스트가 정한다 (잠긴 하네스, 템플릿 frontend/react-vite-ts v1).
//
// - api.route(method, path) 로 계약의 API 를 가로챈다. 들어온 요청은 테스트가 respond/fail/networkError 를 부를 때까지 붙잡아 둔다.
//   실제 시간 지연(sleep)으로 순서를 만들지 않으므로 몇 번을 돌려도 같은 순서로 도착한다.
// - 전역 fetch 를 감싸 "앱이 그 응답을 다 받았는지"(본문을 읽었거나, 요청이 거부됐거나, 오류 응답을 받았는지)를 추적한다.
//   respond/fail/networkError 는 앱이 그 응답을 처리할 기회를 가진 뒤에 돌아온다 — 그 다음의 화면이 판정 대상이다.
// - api.always(...) 는 붙잡지 않고 바로 같은 응답을 준다 (판정과 상관없는 API).
// 손 예시 search-autocomplete-react 의 support.tsx 를 계약의 어느 API 에나 쓰게 일반화했다 (2026-11-09).
// v3 (2026-11-10, 프론트 알파 0108be12 — 오프라인 초안·재연결 동기화 과제):
// - api.collection(path) — 붙잡지 않고 메모리 목록으로 바로 응답하는 목 서버. 서버에 반영됐는지는 api.serverState(path) 로 읽는다
//   (앱이 다시 GET 하기를 기다리지 않는다).
// - network.goOffline()/goOnline() — navigator.onLine 과 offline·online 이벤트, 오프라인 동안 fetch 는 서버에 닿지 않고 거부된다.
// - 경로 변수 이름이 달라도 같은 경로로 본다 ('/drafts/:id' = '/drafts/:draftId' = '/drafts/{draftId}').
// - 상태 204·205·304 응답은 본문 없이 보낸다.
// v6 (2026-11-10): api.once·always·calls 의 요청 본문이 Body(칸 이름으로 읽음) — `(c) => c.body.text === 'B'` 가 컴파일된다 (v5 까지 unknown 이라
//   문서 예시 그대로 쓴 테스트가 TS18046) · api.serverState<T>(path) 가 계약 타입 배열을 돌려준다 (v5 는 Record[] 라 `as 계약타입[]` 이 TS2352).
// v4 (2026-11-10, 프론트 재료 4건): api.once — 다음 한 번만 다른 응답 (collection 의 일부 요청만 실패시킬 때) · call.headers — 요청 헤더
//   (소문자 이름, 예 call.headers.authorization — 다시 보낸 요청이 새 토큰을 쓰는지).
// v8 (2026-11-11, 프론트 알파 fe8 — 오프라인·토큰·로그아웃 재료):
// - api.contract({ ok, testIds }) — contract.ts 가 import 될 때 계약의 성공 상태·test id 를 알린다. 상태를 주지 않은 응답
//   (respond(본문)·always·once·collection)은 계약의 성공 상태로 나간다 (v7 까지 200 — 계약이 201 인 생성 API 에 200 을 보내
//   `res.status === 201` 로 확인하는 허용 구현만 떨어졌다. 실패 메시지에 상태가 안 드러나 수리가 같은 테스트를 다시 만들었다).
// - api.collection 의 toItem·items 가 계약 타입을 받는다 (제네릭 — v7 은 Record 고정이라 `(body: 계약요청) => 계약응답` 이 TS2322).
// - match 없는 기다림의 '모호함'은 서로 다른 요청(메서드·경로·쿼리)이 둘 이상일 때만 — 같은 요청 여럿은 한 응답이 모두에 간다
//   (v7 은 같은 요청 여럿도 오류라, 동시에 같은 갱신 요청을 여러 번 보내는 흔한 구현이 의도한 사유 대신 테스트 결함이 됐다).
// - 요청이 오지 않아 기다림이 끝나면, 응답하지 않은 채 붙잡힌 요청을 함께 알린다 (앞 응답을 받아야 다음 요청을 보내는 구현은
//   그 응답 전에는 다음 요청을 보내지 않는다 — fe8 오프라인 hc2 가 첫 요청을 붙잡은 채 두 번째를 기다려 모든 구현에서 시간 초과).
import { act, waitFor } from '@testing-library/react';
import { http, HttpResponse, type HttpHandler } from 'msw';
import { setupServer } from 'msw/node';

export type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

/** 붙잡힌 요청에 줄 응답 — status 기본은 계약의 성공 상태(계약에 없으면 200), json 본문, networkError 면 연결 실패(fetch 가 거부됨). */
export interface Reply {
  status?: number;
  json?: unknown;
  networkError?: boolean;
}

/** JSON 본문 — 칸 이름으로 읽을 수 있다 (`call.body.text` 는 unknown 이라 비교·String() 으로 쓴다). */
export type Body = Record<string, unknown>;

/** 본문 타입을 정한 요청 기록 — `api.once`·`api.always`·`api.calls` 의 match·결과 (기본 Body). */
export type CallWith<B = Body> = Omit<Call, 'body'> & { body: B };

/** 서버가 받은 요청 하나 (경로 변수·쿼리·본문 포함). */
export interface Call {
  method: Method;
  route: string;
  url: URL;
  params: Record<string, string>;
  query: URLSearchParams;
  /** 요청 헤더 — 이름은 소문자 (예 `call.headers.authorization`). */
  headers: Record<string, string>;
  body: unknown;
}

interface HeldCall extends Call {
  key: string;
  replied: boolean;
  aborted: boolean; // 앱이 요청을 취소함 — match 없는 기다림의 모호함 계산에서 뺀다 (응답은 그대로 줄 수 있다)
  resolve: (reply: Reply) => void;
}

interface ClientCall {
  key: string;
  state: 'pending' | 'rejected' | 'resolved' | 'consumed';
  ok?: boolean;
  at?: number; // 응답을 받은 시각 — 본문을 읽지 않는 앱(상태 코드만 보는 저장 등)도 잠깐 뒤엔 처리한 것으로 본다
}

const GRACE_MS = 150;

interface Standing {
  method: Method;
  route: string;
  match: (call: Call) => boolean;
  reply: Reply;
  left?: number; // api.once — 남은 횟수 (없으면 계속)
}

/** api.collection 이 바로 응답할 때 쓰는 설정 — T 는 처음 항목(items)의 타입, B 는 POST·PUT 요청 본문(계약의 요청 타입).
 *  toItem 의 반환은 어떤 객체든 받는다 — T 를 toItem 반환으로 정하면 items 에 toItem 이 모르는 칸이 있을 때 막히고(TS2353),
 *  계약 타입이 interface 면 Record 에 넣을 수 없다. */
export interface CollectionOptions<T extends object = Body, B = Body> {
  /** 처음 서버에 있는 항목들 (기본 없음). */
  items?: T[];
  /** 항목 id 필드 이름 (기본 'id'). */
  idField?: string;
  /** POST·PUT 본문 → 서버에 저장할 항목 (기본: 본문 그대로 + id). 계약의 응답 모양(updatedAt 등 서버가 채우는 칸)에 맞출 때.
   *  계약 타입을 그대로 쓴다: `toItem: (body: PostApiMemosRequest['body'], id): PostApiMemosOk => ({ ...body, id })`. */
  toItem?: (body: B, id: string) => object;
  /** GET 목록 응답 본문 (기본: 항목 배열 그대로). 계약이 `{ items: [...] }` 처럼 감싸면 여기서. */
  listBody?: (items: T[]) => unknown;
  /** POST 성공 상태 (기본: 계약의 성공 상태, 계약에 없으면 201). */
  createdStatus?: number;
}

interface Collection {
  path: string;
  idField: string;
  items: Body[];
  opts: CollectionOptions;
}

/** 계약의 성공 상태 — contract.ts 가 api.contract 로 알린다 (테스트마다 지우지 않는다: 계약은 파일 하나 동안 같다). */
interface OkStatus {
  method: string;
  route: string;
  status: number;
}
const okStatuses: OkStatus[] = [];
const contractTestIds = new Set<string>();

/** 계약에 적힌 그 API 의 성공 상태 (없으면 undefined). */
function okFor(method: string, route: string): number | undefined {
  return okStatuses.find((s) => s.method === method && sameRoute(s.route, route))?.status;
}

/** 계약에 있는 test id 인지 — 화면 도우미가 문자열 인자 중 범위(test id)를 고를 때. */
export function isContractTestId(id: string): boolean {
  return contractTestIds.has(id);
}

const held: HeldCall[] = [];
const received: Call[] = [];
const client: ClientCall[] = [];
const standing: Standing[] = [];
const routes = new Map<string, HttpHandler>();
const unhandled: string[] = [];
const collections: Collection[] = [];
const blocked: string[] = [];
const server = setupServer();
let originalFetch: typeof fetch | undefined;
let online = true;

const BASE = 'http://localhost';

/** 경로 변수 이름을 지운 모양 — '/drafts/:id' 와 '/drafts/{draftId}' 는 같은 경로. */
function shapeOf(route: string): string {
  return route.replace(/\{[^}/]+\}|:[^/]+/g, ':');
}

function sameRoute(a: string, b: string): boolean {
  return shapeOf(a) === shapeOf(b);
}

/** MSW 꼴 경로 — 계약 꼴 '{name}' 도 받는다. */
function mswPath(route: string): string {
  return route.replace(/\{([^}/]+)\}/g, ':$1');
}

function keyOf(method: string, url: string): string {
  const u = new URL(url, BASE);
  return `${method.toUpperCase()} ${u.pathname}${u.search}`;
}

function requestOf(input: RequestInfo | URL, init?: RequestInit): { method: string; url: string } | null {
  if (typeof input === 'string') return { method: init?.method ?? 'GET', url: input };
  if (input instanceof URL) return { method: init?.method ?? 'GET', url: input.href };
  if (input && typeof (input as Request).url === 'string') {
    return { method: init?.method ?? (input as Request).method ?? 'GET', url: (input as Request).url };
  }
  return null;
}

function headersOf(request: Request): Record<string, string> {
  const out: Record<string, string> = {};
  request.headers.forEach((v, k) => {
    out[k.toLowerCase()] = v;
  });
  return out;
}

async function bodyOf(request: Request): Promise<unknown> {
  const text = await request.text().catch(() => '');
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

const NO_BODY = new Set([204, 205, 304]);

function toResponse(reply: Reply, fallback = 200): Response {
  if (reply.networkError) return HttpResponse.error();
  const status = reply.status ?? fallback;
  if (NO_BODY.has(status)) return new HttpResponse(null, { status });
  return HttpResponse.json((reply.json ?? null) as never, { status });
}

const NOT_FOUND = { code: 'NOT_FOUND', message: '항목이 없습니다' };

function copy<T>(v: T): T {
  return v === undefined ? v : (JSON.parse(JSON.stringify(v)) as T);
}

function newId(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c?.randomUUID) return c.randomUUID();
  return `id-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** api.collection 이 맡은 요청이면 메모리 목록으로 응답 — 아니면 null. */
function serveFromCollection(method: Method, pathname: string, body: unknown): Reply | null {
  for (const c of collections) {
    const base = c.path.replace(/\/+$/, '');
    const rest = pathname.endsWith(base) ? '' : null;
    const m = rest === null ? new RegExp(`${base.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/([^/]+)$`).exec(pathname) : null;
    if (rest === null && m === null) continue;
    const data = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;
    const make = (id: string): Body =>
      c.opts.toItem ? (c.opts.toItem(copy(data), id) as Body) : { ...copy(data), [c.idField]: id };
    if (m === null) {
      if (method === 'GET') return { json: c.opts.listBody ? c.opts.listBody(copy(c.items)) : copy(c.items) };
      if (method === 'POST') {
        const item = make(String(data[c.idField] ?? newId()));
        c.items.push(item);
        return { status: c.opts.createdStatus ?? okFor('POST', base) ?? 201, json: copy(item) };
      }
      return null;
    }
    const id = decodeURIComponent(m[1]);
    const at = c.items.findIndex((it) => String(it[c.idField]) === id);
    if (at < 0) return { status: 404, json: NOT_FOUND };
    if (method === 'GET') return { json: copy(c.items[at]) };
    if (method === 'PUT') {
      c.items[at] = { ...make(id), [c.idField]: c.items[at][c.idField] };
      return { json: copy(c.items[at]) };
    }
    if (method === 'PATCH') {
      c.items[at] = { ...c.items[at], ...copy(data), [c.idField]: c.items[at][c.idField] };
      return { json: copy(c.items[at]) };
    }
    if (method === 'DELETE') {
      c.items.splice(at, 1);
      return { status: okFor('DELETE', `${base}/:`) ?? 204 };
    }
    return null;
  }
  return null;
}

function handlerFor(method: Method, route: string): HttpHandler {
  const make = { GET: http.get, POST: http.post, PUT: http.put, PATCH: http.patch, DELETE: http.delete }[method];
  return make(`*${mswPath(route)}`, async ({ request, params }) => {
    const url = new URL(request.url);
    const call: Call = {
      method,
      route,
      url,
      params: Object.fromEntries(Object.entries(params).map(([k, v]) => [k, String(v)])),
      query: url.searchParams,
      headers: headersOf(request),
      body: await bodyOf(request),
    };
    received.push(call);
    const fixed = standing.find(
      (s) => s.method === method && sameRoute(s.route, route) && (s.left ?? 1) > 0 && s.match(call),
    );
    if (fixed?.left !== undefined) fixed.left -= 1;
    const reply =
      fixed?.reply ??
      serveFromCollection(method, url.pathname, call.body) ??
      (await new Promise<Reply>((resolve) => {
        const entry: HeldCall = { ...call, key: keyOf(method, request.url), replied: false, aborted: false, resolve };
        request.signal?.addEventListener('abort', () => {
          entry.aborted = true;
        });
        held.push(entry);
      }));
    return toResponse(reply, okFor(method, route) ?? 200); // 상태를 주지 않은 응답은 계약의 성공 상태로
  });
}

function installFetchTracker(): void {
  const inner = globalThis.fetch;
  originalFetch = inner;
  const tracked = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const req = requestOf(input, init);
    if (req === null) return inner(input, init);
    const entry: ClientCall = { key: keyOf(req.method, req.url), state: 'pending' };
    client.push(entry);
    if (!online) {
      // 연결이 끊긴 동안의 요청은 서버에 닿지 않는다 — 브라우저처럼 TypeError 로 거부
      entry.state = 'rejected';
      blocked.push(entry.key);
      throw new TypeError('Failed to fetch');
    }
    let res: Response;
    try {
      res = await inner(input, init);
    } catch (e) {
      entry.state = 'rejected';
      throw e;
    }
    entry.state = 'resolved';
    entry.ok = res.ok;
    entry.at = Date.now();
    for (const m of ['json', 'text', 'arrayBuffer', 'blob', 'formData'] as const) {
      const orig = (res as unknown as Record<string, (...a: unknown[]) => Promise<unknown>>)[m].bind(res);
      Object.defineProperty(res, m, {
        configurable: true,
        value: async (...a: unknown[]) => {
          try {
            return await orig(...a);
          } finally {
            entry.state = 'consumed';
          }
        },
      });
    }
    return res;
  };
  globalThis.fetch = tracked as typeof fetch;
}

/** 대기 중인 타이머·마이크로태스크를 몇 바퀴 돌리고 React 갱신을 비운다 (응답은 보내지 않는다). */
export async function flush(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 10; i += 1) await new Promise((r) => setTimeout(r, 0));
  });
}

/** 앱이 key 요청의 응답을 다 처리할 때까지 (본문 읽음 · 거부 · 오류 응답) 기다린 뒤 React 갱신을 비운다. */
async function settle(key: string): Promise<void> {
  const deadline = Date.now() + 3000;
  let fellBack = false;
  await waitFor(
    () => {
      const mine = client.filter((c) => c.key === key);
      const done =
        mine.length > 0 &&
        mine.every(
          (c) =>
            c.state === 'rejected' ||
            c.state === 'consumed' ||
            (c.state === 'resolved' && (c.ok === false || Date.now() - (c.at ?? 0) >= GRACE_MS)),
        );
      if (done) return;
      if (Date.now() >= deadline) {
        fellBack = true;
        return;
      }
      throw new Error(`앱이 응답을 처리하기를 기다림: ${key}`);
    },
    { timeout: 5000, interval: 10 },
  );
  if (fellBack) console.warn(`[grader] settle fallback ${key} (fetch 추적 불가 — 3초 대기 후 판정)`);
  await flush();
}

/** 붙잡힌 요청 하나 — 응답을 보내고 앱이 처리할 때까지 기다린다. 같은 요청(메서드·경로·쿼리)이 여럿이면 모두에 같은 응답. */
export interface CallHandle extends Call {
  /** 응답 — `respond(본문)`·`respond(본문, 201)`, 상태를 먼저 쓴 `respond(404, 본문)` 도 받는다. */
  respond(json: unknown, status?: number): Promise<void>;
  respond(status: number, json: unknown): Promise<void>;
  /** 오류 응답 — `fail()`·`fail(503)`·`fail(409, 본문)`, 또는 본문만 `fail({ code, message })` (상태 500). */
  fail(status?: number, json?: unknown): Promise<void>;
  fail(json: object): Promise<void>;
  networkError(): Promise<void>;
}

function replyAll(key: string, reply: Reply): void {
  for (const c of held) {
    if (c.key === key && !c.replied) {
      c.replied = true;
      c.resolve(reply);
    }
  }
}

function handle(call: HeldCall): CallHandle {
  return {
    method: call.method,
    route: call.route,
    url: call.url,
    params: call.params,
    query: call.query,
    headers: call.headers,
    body: call.body,
    async respond(a: unknown, b?: unknown) {
      // 상태를 먼저 쓴 꼴(respond(500, { message }))도 받는다 — 2026-11-09 상류 평가·알파: 공개 테스트가 이렇게 써 타입 오류 4건
      const swapped = typeof a === 'number' && b !== undefined && typeof b !== 'number';
      // 상태를 주지 않으면 계약의 성공 상태 (v8 — 계약이 201 인 생성 API 에 200 을 보내던 v7 의 결함)
      const [json, status] = swapped ? [b, a as number] : [a, (b as number | undefined) ?? okFor(call.method, call.route) ?? 200];
      replyAll(call.key, { json, status });
      await settle(call.key);
    },
    async fail(
      statusOrBody: number | object = 500,
      json: unknown = { code: 'INTERNAL', message: '일시적인 오류' },
    ) {
      // 본문만 준 꼴(fail({ code, message }))도 받는다 — 2026-11-09 상류 평가: 공개 테스트 3/3 이 이렇게 써 타입 오류
      const [status, body] =
        typeof statusOrBody === 'number' ? [statusOrBody, json] : [500, statusOrBody];
      replyAll(call.key, { json: body, status });
      await settle(call.key);
    },
    async networkError() {
      replyAll(call.key, { networkError: true });
      await settle(call.key);
    },
  };
}

export const api = {
  /** 이 과제의 계약을 알린다 — contract.ts 가 import 될 때 부른다 (테스트가 직접 부르지 않는다).
   *  ok: 'METHOD /path' → 성공 상태 (상태를 주지 않은 응답이 이 상태로 나간다) · testIds: 계약의 화면 요소 test id. */
  contract(meta: { ok?: Record<string, number>; testIds?: readonly string[] }): void {
    for (const [k, status] of Object.entries(meta.ok ?? {})) {
      const [m, route] = k.split(/\s+/, 2);
      if (!m || !route) continue;
      const method = m.toUpperCase();
      const at = okStatuses.findIndex((s) => s.method === method && sameRoute(s.route, route));
      if (at >= 0) okStatuses.splice(at, 1);
      okStatuses.push({ method, route, status });
    }
    for (const id of meta.testIds ?? []) contractTestIds.add(id);
  },
  /** 계약의 API 하나를 가로챈다 (경로는 MSW 꼴 — 예: '/api/items/:id'). 같은 메서드·경로는 한 번만. */
  route(method: Method, route: string): void {
    const k = `${method} ${shapeOf(route)}`;
    if (routes.has(k)) return;
    const h = handlerFor(method, route);
    routes.set(k, h);
    server.use(h);
  },
  /** 붙잡지 않고 바로 응답할 요청 (판정과 상관없는 API). match 를 주면 그 요청만. */
  always<B = Body>(method: Method, route: string, reply: Reply, match: (call: CallWith<B>) => boolean = () => true): void {
    api.route(method, route);
    standing.push({ method, route, match: match as (call: Call) => boolean, reply });
  },
  /** 맞는 다음 요청 한 번만 이 응답 — 그 뒤 요청은 원래대로 (collection 이면 목록이, 아니면 붙잡기). 예: 대기열을 다시 보낼 때
   *  변경 하나만 실패 `api.once('POST', '/notes', { status: 500, json: { code: 'E', message: '저장 실패' } }, (c) => c.body.text === 'B')`. */
  once<B = Body>(method: Method, route: string, reply: Reply, match: (call: CallWith<B>) => boolean = () => true): void {
    api.route(method, route);
    standing.push({ method, route, match: match as (call: Call) => boolean, reply, left: 1 });
  },
  /** 서버가 (아직 응답하지 않은) 맞는 요청을 받을 때까지 기다린다. match 로 쿼리·본문을 고른다. */
  async waitForCall(
    method: Method,
    route: string,
    match?: (call: Call) => boolean,
    timeout = 5000,
  ): Promise<CallHandle> {
    const pick = match ?? (() => true);
    const served = collections.find((c) => sameRoute(c.path, route) || sameRoute(`${c.path.replace(/\/+$/, '')}/:`, route));
    if (served) {
      throw new Error(
        `${method} ${route} 는 api.collection('${served.path}') 이 바로 응답한다 — 응답 시점을 정할 요청은 collection 없이 waitForCall 로, ` +
          `서버에 반영됐는지는 api.serverState('${served.path}') 로 확인한다`,
      );
    }
    let found: HeldCall | undefined;
    await waitFor(
      () => {
        found = held.find((c) => c.method === method && sameRoute(c.route, route) && !c.replied && pick(c));
        if (!found) {
          const seen = received.map((c) => `${c.method} ${c.url.pathname}${c.url.search}`);
          const off = blocked.length ? ` (연결이 끊겨 서버에 닿지 않은 요청: ${JSON.stringify(blocked)})` : '';
          // 응답하지 않은 채 붙잡힌 요청 — 수리 메시지가 길이로 잘려도 남게 받은 요청 목록보다 앞에 (v8, fe8 오프라인 hc2).
          // 같은 경로의 요청이 와 있으면 match 에 맞지 않은 것 — 본문을 보여 준다 (알파 fe9-token hc2: 테스트가 저장소 키를 지어내
          // 갱신 요청 본문의 토큰이 빈 값이었는데, '먼저 응답하라'는 안내만 보였다).
          const open = held.filter((c) => !c.replied && !c.aborted);
          const same = open.filter((c) => c.method === method && sameRoute(c.route, route));
          const brief = (v: unknown) => {
            const t = JSON.stringify(v ?? null);
            return t.length > 80 ? `${t.slice(0, 79)}…` : t;
          };
          const miss = same.length
            ? ` — 같은 경로의 요청 ${same.length}개가 왔지만 match 에 맞지 않았다 (본문 ${same.slice(0, 2).map((c) => brief(c.body)).join(', ')})`
            : '';
          const wait = open.length
            ? ` — 응답하지 않은 요청 ${open.length}개가 붙잡혀 있는 동안 기다렸다 ${JSON.stringify(open.slice(0, 3).map((c) => `${c.method} ${c.url.pathname}${c.url.search}`))}` +
              ' (앞 요청의 응답을 받은 뒤 다음 요청을 보내는 구현도 있다 — 먼저 응답할 요청이 있는지 본다)'
            : '';
          throw new Error(`요청이 오지 않음: ${method} ${route}${miss}${wait} (받은 요청: ${JSON.stringify(seen)})${off}`);
        }
      },
      { timeout, interval: 10 },
    );
    if (!match) {
      // match 없이 기다렸는데 붙잡힌 요청이 여럿 — 어느 것에 응답할지 구현마다 달라진다 (v7, 2026-11-11 응답 순서 cf4: '서버'를 치면
      // '서'·'서버' 두 요청이 나가는데 먼저 온 '서'에 응답해, 지난 응답을 무시하는 올바른 구현만 떨어졌다). 취소된 요청은 세지 않는다.
      // v8: 같은 요청(메서드·경로·쿼리)이 여럿이면 한 응답이 모두에 가므로 모호하지 않다 — 다른 요청이 둘 이상일 때만 오류
      // (2026-11-11 알파 fe9-token: 흔한 구현이 동시에 보낸 같은 갱신 요청 4개 — 함정 그 자체 — 를 모호한 기다림으로 짚어,
      // 의도한 사유로 떨어져야 할 흔한 구현이 '테스트 결함'이 됐다).
      const live = held.filter((c) => c.method === method && sameRoute(c.route, route) && !c.replied && !c.aborted);
      const keys = [...new Set(live.map((c) => c.key))];
      if (keys.length > 1) {
        const which = keys.map((k) => k.slice(k.indexOf(' ') + 1));
        throw new Error(
          `붙잡힌 ${method} ${route} 요청이 ${keys.length}개 — match 로 확인할 요청을 고른다 (예: (c) => c.query.get('q') === '지금 입력'): ${JSON.stringify(which)}`,
        );
      }
      if (live.length > 0) found = live[0]; // 취소된 지난 요청보다 살아 있는 요청 (match 를 주면 취소된 요청도 고를 수 있다)
    }
    return handle(found as HeldCall);
  },
  /** 지금까지 받은 요청 (응답 여부와 무관, 받은 순서). */
  calls<B = Body>(method?: Method, route?: string): CallWith<B>[] {
    return received.filter(
      (c) => (!method || c.method === method) && (!route || sameRoute(c.route, route)),
    ) as CallWith<B>[];
  },
  /** 아직 응답하지 않은 요청 수. */
  pending(method?: Method, route?: string): number {
    return held.filter((c) => !c.replied && (!method || c.method === method) && (!route || sameRoute(c.route, route))).length;
  },
  /**
   * 목 서버 목록 하나 — path(예 '/drafts') 와 path/:id 의 요청을 붙잡지 않고 메모리 목록으로 바로 응답한다.
   * GET path → 목록 · POST path → 항목 추가(본문에 id 가 없으면 서버가 정함, 계약의 성공 상태 — 없으면 201) · GET/PUT/PATCH/DELETE path/:id → 항목 하나(없으면 404, DELETE 는 계약의 성공 상태 — 없으면 204).
   * 같은 id 로 두 번 POST 하면 두 번 들어간다 (중복 전송이 serverState 에 보인다).
   */
  collection<T extends object = Body, B = Body>(path: string, opts: CollectionOptions<T, B> = {}): void {
    const base = path.replace(/\/+$/, '');
    for (const m of ['GET', 'POST'] as const) api.route(m, base);
    for (const m of ['GET', 'PUT', 'PATCH', 'DELETE'] as const) api.route(m, `${base}/:id`);
    const i = collections.findIndex((c) => c.path === base);
    const loose = opts as unknown as CollectionOptions; // 안에서는 칸 이름으로 다룬다 — 모양은 테스트가 계약 타입으로 정했다
    const c: Collection = { path: base, idField: opts.idField ?? 'id', items: copy(loose.items ?? []), opts: loose };
    if (i >= 0) collections[i] = c;
    else collections.push(c);
  },
  /** api.collection 목록의 지금 서버 상태 (복사본, 들어온 순서). 앱이 다시 GET 하기를 기다리지 않고 서버에 반영됐는지 본다. */
  serverState<T = Body>(path: string): T[] {
    const base = path.replace(/\/+$/, '');
    const c = collections.find((x) => x.path === base);
    if (!c) {
      const known = collections.map((x) => x.path);
      throw new Error(`serverState('${path}') — api.collection('${path}') 으로 만든 목록이 없다 (있는 목록: ${JSON.stringify(known)})`);
    }
    return copy(c.items) as unknown as T[]; // 모양은 계약이 정한다 — 테스트가 계약 타입으로 받는다 (api.serverState<Memo>(…))
  },
  /** 계약에 없는(가로채지 않은) 요청 — 앱이 계약 밖 API 를 부르면 여기에 남는다. */
  unhandled(): string[] {
    return [...unhandled];
  },
  listen(): void {
    // navigator.onLine 을 network.goOffline/goOnline 이 정한 값으로 (jsdom 은 늘 true)
    for (const nav of new Set([window.navigator, globalThis.navigator])) {
      if (nav) Object.defineProperty(nav, 'onLine', { configurable: true, get: () => online });
    }
    server.listen({
      onUnhandledRequest(request) {
        unhandled.push(`${request.method} ${new URL(request.url).pathname}`);
      },
    });
    installFetchTracker();
  },
  close(): void {
    if (originalFetch) globalThis.fetch = originalFetch;
    server.close();
  },
  /** 테스트마다: 붙잡힌 요청을 빈 응답으로 풀고 기록·고정 응답을 지운다 (가로챈 경로는 그대로). */
  async reset(): Promise<void> {
    for (const c of held) {
      if (!c.replied) {
        c.replied = true;
        c.resolve({ status: 200, json: null });
      }
    }
    held.length = 0;
    received.length = 0;
    client.length = 0;
    standing.length = 0;
    unhandled.length = 0;
    collections.length = 0;
    blocked.length = 0;
    online = true;
    await new Promise((r) => setTimeout(r, 0));
  },
};

async function announce(type: 'online' | 'offline'): Promise<void> {
  await act(async () => {
    window.dispatchEvent(new Event(type));
  });
  await flush();
}

/** 브라우저 연결 상태 — 오프라인 저장·재연결 동기화를 시험할 때. 테스트마다 온라인으로 돌아간다. */
export const network = {
  /** 연결을 끊는다: navigator.onLine = false, window 에 'offline' 이벤트. 그 뒤 fetch 는 서버에 닿지 않고 TypeError 로 거부된다
   *  (이미 붙잡힌 요청은 그대로). */
  async goOffline(): Promise<void> {
    online = false;
    await announce('offline');
  },
  /** 다시 연결한다: navigator.onLine = true, window 에 'online' 이벤트 (앱의 동기화가 여기서 시작된다). 동기화가 끝났는지는
   *  `await waitFor(() => expect(api.serverState('/drafts')).toEqual(…))` 처럼 서버 상태로 기다린다. */
  async goOnline(): Promise<void> {
    online = true;
    await announce('online');
  },
  /** 지금 연결돼 있는지. */
  isOnline(): boolean {
    return online;
  },
  /** 연결이 끊긴 동안 앱이 보내려다 거부된 요청 ('METHOD /path?query'). */
  blocked(): string[] {
    return [...blocked];
  },
};
