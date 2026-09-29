<script setup lang="ts">
/**
 * 应用外壳：左侧导航轨 + 底部后端状态。
 *
 * 视觉语言取自 linear.app 的设计规范（暗色 canvas #010102、四段表面阶梯、
 * 单一 lavender 强调色、1px hairline 分组）。没有顶栏 —— 页面自己管标题，
 * 少一层容器就少一层 padding，密集工具界面里这点很值。
 */
import { PhMoon, PhSun } from '@phosphor-icons/vue'

const ui = useUiStore()
const route = useRoute()

const nav = [
  { to: '/', label: '对话', hint: '多智能体问答' },
  { to: '/papers', label: '文献库', hint: '上传 · 解析 · 分块' },
  { to: '/graph', label: '引文图谱', hint: 'PageRank · 社群 · 路径' },
  { to: '/writing', label: '写作', hint: '草稿 · 翻译' },
  { to: '/tools', label: '工具', hint: 'MCP 直调台' },
]

const healthText = computed(() => {
  const h = ui.health
  if (!h) return ui.healthError ? '后端不可达' : '检查中…'
  const bad = h.components.filter((c) => !c.ok).map((c) => c.name)
  return bad.length ? `降级 · ${bad.join(' / ')} 不可用` : '全部组件正常'
})

useHead({
  htmlAttrs: { 'data-theme': computed(() => ui.theme) },
})

let timer: ReturnType<typeof setInterval> | null = null
onMounted(() => {
  // 主题存在 localStorage：SSR 时读不到，客户端接管后立刻纠正，不会闪太久
  ui.restoreTheme()
  timer = setInterval(() => void ui.refreshHealth(), 30_000)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="shell">
    <aside class="rail">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true">R</span>
        <span class="brand-text">
          <span class="brand-name">Research Copilot</span>
          <span class="brand-sub">多智能体学术助手</span>
        </span>
      </div>

      <nav class="nav" aria-label="主导航">
        <NuxtLink
          v-for="item in nav"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          :class="{ active: route.path === item.to }"
          :aria-current="route.path === item.to ? 'page' : undefined"
        >
          <span class="nav-label">{{ item.label }}</span>
          <span class="nav-hint">{{ item.hint }}</span>
        </NuxtLink>
      </nav>

      <footer class="rail-foot">
        <div class="rc-row">
          <span class="rc-dot" :class="`rc-dot--${ui.healthLight}`" />
          <span class="rc-mono rc-muted">{{ ui.healthLight }}</span>
          <span class="rc-spacer" />
          <button
            class="rc-btn rc-btn--ghost rc-btn--icon rc-btn--sm"
            type="button"
            :aria-label="ui.theme === 'dark' ? '切换到浅色主题' : '切换到暗色主题'"
            :title="ui.theme === 'dark' ? '浅色主题' : '暗色主题'"
            @click="ui.toggleTheme()"
          >
            <PhMoon v-if="ui.theme === 'dark'" :size="14" />
            <PhSun v-else :size="14" />
          </button>
        </div>
        <p class="foot-note">{{ healthText }}</p>
        <p v-if="ui.health" class="rc-mono foot-note">v{{ ui.health.version }} · {{ ui.health.env }}</p>
      </footer>
    </aside>

    <main class="body">
      <slot />
    </main>
  </div>
</template>

<style scoped>
.shell {
  display: grid;
  grid-template-columns: 208px 1fr;
  height: 100vh;
  background: var(--rc-canvas);
}

.rail {
  display: flex;
  flex-direction: column;
  background: var(--rc-canvas);
  border-right: 1px solid var(--rc-hairline);
}

.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  height: 52px;
  padding: 0 14px;
  border-bottom: 1px solid var(--rc-hairline);
}
.brand-mark {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  flex: 0 0 22px;
  border-radius: var(--rc-radius-sm);
  background: var(--rc-primary);
  color: var(--rc-on-primary);
  font-family: var(--rc-font-display);
  font-size: 12px;
  font-weight: 600;
}
.brand-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.brand-name {
  font-family: var(--rc-font-display);
  font-size: 12.5px;
  font-weight: 600;
  letter-spacing: -0.2px;
  line-height: 1.3;
}
.brand-sub {
  font-size: 10.5px;
  color: var(--rc-ink-tertiary);
  line-height: 1.3;
}

.nav {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 8px;
}
.nav-item {
  display: block;
  padding: 6px 9px;
  border-radius: var(--rc-radius-md);
  color: var(--rc-ink-muted);
  text-decoration: none;
}
.nav-item:hover {
  background: var(--rc-surface-1);
  text-decoration: none;
}
.nav-item.active {
  background: var(--rc-surface-2);
}
.nav-label {
  display: block;
  font-size: 13px;
  line-height: 1.4;
}
.nav-item.active .nav-label {
  color: var(--rc-ink);
  font-weight: 500;
}
.nav-hint {
  display: block;
  font-size: 10.5px;
  color: var(--rc-ink-tertiary);
  line-height: 1.4;
}

.rail-foot {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 10px 12px;
  border-top: 1px solid var(--rc-hairline);
}
.foot-note {
  margin: 0;
  font-size: 11px;
  color: var(--rc-ink-tertiary);
  line-height: 1.45;
}

.body {
  min-width: 0;
  overflow: hidden;
  background: var(--rc-canvas);
}
</style>
