// 응시용 테스트 설정 — jsdom 에 없는 IndexedDB 는 fake-indexeddb 로 채운다 (테스트마다 빈 저장소).
import 'fake-indexeddb/auto';
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { IDBFactory } from 'fake-indexeddb';
import { afterAll, afterEach, beforeAll, beforeEach } from 'vitest';
import { server } from '../mocks/server';

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
// 테스트마다 빈 브라우저 저장소 (IndexedDB·localStorage·sessionStorage). 한 테스트 안에서 renderApp 을 다시 부르는 '새로고침'은 저장소를 그대로 둔다.
beforeEach(() => {
  globalThis.indexedDB = new IDBFactory() as unknown as typeof globalThis.indexedDB;
  window.localStorage.clear();
  window.sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());
