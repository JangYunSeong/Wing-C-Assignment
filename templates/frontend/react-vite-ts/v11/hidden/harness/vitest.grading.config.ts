// 채점 전용 vitest 설정 — 지원자 vite 설정(plugins·resolve)은 그대로 쓰고 test 설정만 채점용으로 바꾼다.
//   npx vitest run --config src/__grading__/harness/vitest.grading.config.ts
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { loadConfigFromFile, type UserConfig } from 'vite';
import { defineConfig } from 'vitest/config';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../../..');

export default defineConfig(async (env) => {
  const loaded = await loadConfigFromFile(env, undefined, root).catch(() => null);
  const base: UserConfig = loaded?.config ?? {};
  const { test: _ignored, ...rest } = base as UserConfig & { test?: unknown };
  return {
    ...rest,
    root,
    plugins: rest.plugins ?? [react()],
    test: {
      environment: 'jsdom',
      include: [
        'src/__grading__/selftest/**/*.test.{ts,tsx}',
        'src/__grading__/cases/**/*.test.{ts,tsx}',
        'src/__public__/**/*.test.{ts,tsx}',
      ],
      setupFiles: ['src/__grading__/harness/grader-setup.ts'],
      testTimeout: 30_000,
      hookTimeout: 30_000,
    },
  };
});
