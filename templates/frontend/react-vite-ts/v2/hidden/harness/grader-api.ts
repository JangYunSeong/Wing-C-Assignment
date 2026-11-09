// 채점 하네스 — 서버 API 를 가로채 응답 시점을 테스트가 정한다 (잠긴 하네스, 템플릿 frontend/react-vite-ts v1).
//
// - api.route(method, path) 로 계약의 API 를 가로챈다. 들어온 요청은 테스트가 respond/fail/networkError 를 부를 때까지 붙잡아 둔다.
//   실제 시간 지연(sleep)으로 순서를 만들지 않으므로 몇 번을 돌려도 같은 순서로 도착한다.
// - 전역 fetch 를 감싸 "앱이 그 응답을 다 받았는지"(본문을 읽었거나, 요청이 거부됐거나, 오류 응답을 받았는지)를 추적한다.
//   respond/fail/networkError 는 앱이 그 응답을 처리할 기회를 가진 뒤에 돌아온다 — 그 다음의 화면이 판정 대상이다.
// - api.always(...) 는 붙잡지 않고 바로 같은 응답을 준다 (판정과 상관없는 API).
// 손 예시 search-autocomplete-react 의 support.tsx 를 계약의 어느 API 에나 쓰게 일반화했다 (2026-11-09).
import { act, waitFor } from '@testing-library/react';
import { http, HttpResponse, type HttpHandler } from 'msw';
import { setupServer } from 'msw/node';

export type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

/** 붙잡힌 요청에 줄 응답 — status 기본 200, json 본문, networkError 면 연결 실패(fetch 가 거부됨). */
export interface Reply {
  status?: number;
  json?: unknown;
  networkError?: boolean;
}

/** 서버가 받은 요청 하나 (경로 변수·쿼리·본문 포함). */
export interface Call {
  method: Method;
  route: string;
  url: URL;
  params: Record<string, string>;
  query: URLSearchParams;
  body: unknown;
}

interface HeldCall extends Call {
  key: string;
  replied: boolean;
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
}

const held: HeldCall[] = [];
const received: Call[] = [];
const client: ClientCall[] = [];
const standing: Standing[] = [];
const routes = new Map<string, HttpHandler>();
const unhandled: string[] = [];
const server = setupServer();
let originalFetch: typeof fetch | undefined;

const BASE = 'http://localhost';

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

async function bodyOf(request: Request): Promise<unknown> {
  const text = await request.text().catch(() => '');
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function toResponse(reply: Reply): Response {
  if (reply.networkError) return HttpResponse.error();
  return HttpResponse.json((reply.json ?? null) as never, { status: reply.status ?? 200 });
}

function handlerFor(method: Method, route: string): HttpHandler {
  const make = { GET: http.get, POST: http.post, PUT: http.put, PATCH: http.patch, DELETE: http.delete }[method];
  return make(`*${route}`, async ({ request, params }) => {
    const url = new URL(request.url);
    const call: Call = {
      method,
      route,
      url,
      params: Object.fromEntries(Object.entries(params).map(([k, v]) => [k, String(v)])),
      query: url.searchParams,
      body: await bodyOf(request),
    };
    received.push(call);
    const fixed = standing.find((s) => s.method === method && s.route === route && s.match(call));
    const reply =
      fixed?.reply ??
      (await new Promise<Reply>((resolve) => {
        held.push({ ...call, key: keyOf(method, request.url), replied: false, resolve });
      }));
    return toResponse(reply);
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
    body: call.body,
    async respond(a: unknown, b?: unknown) {
      // 상태를 먼저 쓴 꼴(respond(500, { message }))도 받는다 — 2026-11-09 상류 평가·알파: 공개 테스트가 이렇게 써 타입 오류 4건
      const swapped = typeof a === 'number' && b !== undefined && typeof b !== 'number';
      const [json, status] = swapped ? [b, a as number] : [a, (b as number | undefined) ?? 200];
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
  /** 계약의 API 하나를 가로챈다 (경로는 MSW 꼴 — 예: '/api/items/:id'). 같은 메서드·경로는 한 번만. */
  route(method: Method, route: string): void {
    const k = `${method} ${route}`;
    if (routes.has(k)) return;
    const h = handlerFor(method, route);
    routes.set(k, h);
    server.use(h);
  },
  /** 붙잡지 않고 바로 응답할 요청 (판정과 상관없는 API). match 를 주면 그 요청만. */
  always(method: Method, route: string, reply: Reply, match: (call: Call) => boolean = () => true): void {
    api.route(method, route);
    standing.push({ method, route, match, reply });
  },
  /** 서버가 (아직 응답하지 않은) 맞는 요청을 받을 때까지 기다린다. match 로 쿼리·본문을 고른다. */
  async waitForCall(
    method: Method,
    route: string,
    match: (call: Call) => boolean = () => true,
    timeout = 5000,
  ): Promise<CallHandle> {
    let found: HeldCall | undefined;
    await waitFor(
      () => {
        found = held.find((c) => c.method === method && c.route === route && !c.replied && match(c));
        if (!found) {
          const seen = received.map((c) => `${c.method} ${c.url.pathname}${c.url.search}`);
          throw new Error(`요청이 오지 않음: ${method} ${route} (받은 요청: ${JSON.stringify(seen)})`);
        }
      },
      { timeout, interval: 10 },
    );
    return handle(found as HeldCall);
  },
  /** 지금까지 받은 요청 (응답 여부와 무관, 받은 순서). */
  calls(method?: Method, route?: string): Call[] {
    return received.filter((c) => (!method || c.method === method) && (!route || c.route === route));
  },
  /** 아직 응답하지 않은 요청 수. */
  pending(method?: Method, route?: string): number {
    return held.filter((c) => !c.replied && (!method || c.method === method) && (!route || c.route === route)).length;
  },
  /** 계약에 없는(가로채지 않은) 요청 — 앱이 계약 밖 API 를 부르면 여기에 남는다. */
  unhandled(): string[] {
    return [...unhandled];
  },
  listen(): void {
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
    await new Promise((r) => setTimeout(r, 0));
  },
};
