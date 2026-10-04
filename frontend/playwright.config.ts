import { defineConfig, devices } from '@playwright/test'

/**
 * 前端 E2E 配置。
 *
 * 定位：**只打前端自己的渲染、路由与交互，不依赖后端**。
 * 这不是妥协，是刻意的 —— 后端没起时页面必须显示「不可达」而不是白屏，
 * 这条行为本身就该被守住（也正是 typecheck / build 都发现不了的那一类 bug：
 * SSR 能过、客户端 hydration 炸掉）。
 *
 * 端口用 `localhost` 而不是 `127.0.0.1`：dev server 默认只绑 IPv6 的 ::1，
 * 写 127.0.0.1 会连不上（本仓已在 Nuxt 上踩过）。
 *
 * 跑法：
 *     npm run e2e              # 无头
 *     npm run e2e:headed       # 看浏览器
 *     npm run e2e:report       # 看上次失败的 trace / 截图
 */
const PORT = Number(process.env.E2E_PORT ?? 3000)
const BASE_URL = process.env.E2E_BASE_URL ?? `http://localhost:${PORT}`

export default defineConfig({
  testDir: './e2e',
  timeout: 45_000,
  expect: { timeout: 10_000 },

  // 只有一个 dev server，首屏编译很贵：串行 + 单 worker，别让 6 个页面同时触发编译
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,

  reporter: [['list'], ['html', { open: 'never', outputFolder: 'e2e-report' }]],

  use: {
    baseURL: BASE_URL,
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },

  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        // 用「完整 chromium + 新无头模式」而不是 Playwright 默认的 headless shell：
        // shell 是裁过的构建，缺字体/媒体/部分 API，截图与渲染结果会跟真人看到的不一样。
        // 这一层 E2E 的目的正是"看人看到的东西"，所以宁可多花点内存。
        channel: 'chromium',
      },
    },
  ],

  // 已经手动起了 dev server 就直接复用（本机常态），没起则自己拉一个
  webServer: {
    command: 'npm run dev',
    url: BASE_URL,
    reuseExistingServer: true,
    timeout: 180_000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
})
