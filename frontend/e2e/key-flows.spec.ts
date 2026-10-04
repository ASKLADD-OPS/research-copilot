import { expect, test, type Page } from '@playwright/test'

/**
 * 关键流程 E2E。
 *
 * 三条设计原则：
 *
 * 1. **不依赖后端。** 这一层测的是前端自己的渲染、路由与交互。后端没起时页面必须
 *    显示状态徽章而不是白屏 —— 这是要守住的行为，不是要被绕过的障碍。
 * 2. **每条用例都收集未捕获的 JS 异常。** SSR 能跑通、build 能过、typecheck 全绿，
 *    但客户端 hydration 炸掉 —— 这类 bug 只有真在浏览器里跑才看得见（本仓踩过
 *    `DOMPurify` 的默认导出在服务端是工厂函数那个坑）。`expect(pageErrors).toEqual([])`
 *    是这层 E2E 存在的主要理由。
 * 3. **断言用真实可见的东西**：`<title>`（每个页面各自 `useHead` 设的，能证明页面组件
 *    真的渲染了）、h1、链接文字、输入框 placeholder —— 不是 CSS 类名，改样式不该让测试红。
 */

/** 收集未捕获异常。必须在 goto 之前挂上，否则首屏的异常会漏掉。 */
function trackPageErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  return errors
}

function isIgnorable(msg: string): boolean {
  // dev server 的 HMR / vite ws 断连不是产品 bug，别让它把构建卡红
  return /vite|hmr|websocket|WebSocket/i.test(msg) && /connect|closed|failed/i.test(msg)
}

const MARKETING = [
  { path: '/', title: /把一堆论文/, heading: /把一堆论文/ },
  { path: '/pricing', title: /部署与定价/ },
  { path: '/about', title: /关于/ },
] as const

const APP_MODULES = [
  { path: '/app', title: /研究工作台/, label: null },
  { path: '/app/tools', title: /主题探索/, label: '主题探索' },
  { path: '/app/writing', title: /写作台/, label: '写作台' },
  { path: '/app/visualize', title: /数据可视化/, label: '数据可视化' },
  { path: '/app/translate', title: /学术翻译/, label: '学术翻译' },
] as const

// ==================================================================== 营销站
for (const route of MARKETING) {
  test(`营销站 ${route.path} 能渲染，标题自设，无未捕获异常`, async ({ page }) => {
    const errors = trackPageErrors(page)

    const resp = await page.goto(route.path, { waitUntil: 'domcontentloaded' })
    expect(resp?.status(), `${route.path} 没返回 2xx`).toBeLessThan(400)

    await expect(page).toHaveTitle(route.title)

    // 营销站每页都有 h1（SEO 与可访问性的基本盘）
    await expect(page.locator('h1').first()).toBeVisible()

    // 首屏真的挂了东西，不是空壳
    await expect(page.locator('#__nuxt')).not.toBeEmpty()

    expect(errors.filter((m) => !isIgnorable(m))).toEqual([])
  })
}

// ==================================================================== 工作台六模块
for (const mod of APP_MODULES) {
  test(`工作台模块 ${mod.path} 能打开，标题正确`, async ({ page }) => {
    const errors = trackPageErrors(page)

    await page.goto(mod.path, { waitUntil: 'domcontentloaded' })
    await expect(page).toHaveTitle(mod.title)
    await expect(page.locator('#__nuxt')).not.toBeEmpty()

    // 四个独立页都带一个回工作台的导航（它们是这四页唯一的共用骨架，
    // 漂了说明页面壳被改坏了），以及自己的名字（证明渲染的是本页而不是兜底页）
    if (mod.label) {
      await expect(page.getByRole('link', { name: '工作台' })).toBeVisible()
      await expect(page.getByText(mod.label, { exact: true }).first()).toBeVisible()
    }

    expect(errors.filter((m) => !isIgnorable(m))).toEqual([])
  })
}

// ==================================================================== 顶栏健康徽章
test('工作台顶栏的后端健康徽章可点开，文案只在四种状态内', async ({ page }) => {
  const errors = trackPageErrors(page)
  await page.goto('/app', { waitUntil: 'domcontentloaded' })

  // 顶栏品牌名
  await expect(page.getByText('Research Copilot', { exact: true }).first()).toBeVisible()

  // 健康徽章：按 title 前缀定位（结构稳定），再断言文案只在四种状态内。
  // 不按文本定位 —— 按钮里还有个空的圆点 span，文本匹配对空白敏感，脆。
  const badge = page.locator('button[title^="后端："]')
  await expect(badge).toBeVisible()
  await expect(badge).toHaveText(/^\s*(正常|降级\s*\d+\s*项|不可达|未检查)\s*$/)

  // 点开健康面板，后端组件清单要真的出来
  await badge.click()
  await expect(page.getByText('后端组件').first()).toBeVisible()

  expect(errors.filter((m) => !isIgnorable(m))).toEqual([])
})

// ==================================================================== 兜底 404
test('不存在的路径走兜底 404：HTTP 404 + 专属标题', async ({ page }) => {
  const errors = trackPageErrors(page)

  const resp = await page.goto('/this-path-does-not-exist-9f3a', { waitUntil: 'domcontentloaded' })
  expect(resp?.status(), '兜底路由必须回真 404（否则会被搜索引擎收录）').toBe(404)

  await expect(page).toHaveTitle(/页面不存在/)
  await expect(page.locator('#__nuxt')).not.toBeEmpty()

  expect(errors.filter((m) => !isIgnorable(m))).toEqual([])
})
