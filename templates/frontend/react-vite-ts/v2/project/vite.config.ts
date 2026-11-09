// 템플릿 frontend/react-vite-ts v1 — 잠긴 파일 (과제가 바꾸지 않는다). 채점은 이 설정의 plugins·resolve 를 그대로 쓰고 test 설정만 바꾼다.
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    // 채점 테스트(숨은·공개)는 채점용 설정으로만 돈다 (src/__grading__/harness/vitest.grading.config.ts)
    exclude: ['src/__grading__/**', 'src/__public__/**', 'node_modules/**'],
    // jsdom 첫 렌더·user-event 입력이 느린 환경에서도 기본값(5초)에 걸리지 않게
    testTimeout: 15_000,
  },
});
