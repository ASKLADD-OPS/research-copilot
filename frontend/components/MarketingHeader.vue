<script setup lang="ts">
/**
 * 营销站顶栏。吸顶 + 滚动后浮出发丝线与毛玻璃 + 移动端汉堡菜单。
 *
 * 三个容易做错的地方：
 *  1. 滚动监听必须 passive —— 否则移动端滚动会被迫等 JS，掉帧很明显。
 *  2. 汉堡菜单打开后要能用 Esc 关、点链接自动关、切到桌面宽度自动关。
 *     少任何一条，用户就会卡在"菜单关不掉"的状态里。
 *  3. 按钮要带 aria-expanded / aria-controls，否则读屏用户不知道它是个开关。
 */
import { PhList, PhX, PhArrowRight } from '@phosphor-icons/vue'

const nav = [
  { label: '功能', to: '/#features' },
  { label: '定价', to: '/pricing' },
  { label: '关于', to: '/about' },
] as const

const route = useRoute()
const scrolled = ref(false)
const menuOpen = ref(false)

function onScroll() {
  scrolled.value = window.scrollY > 8
}

/** 桌面宽度下强制收起抽屉，避免"缩放窗口后菜单悬空开着"。 */
function onResize() {
  if (window.innerWidth >= 1024) menuOpen.value = false
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') menuOpen.value = false
}

onMounted(() => {
  onScroll()
  window.addEventListener('scroll', onScroll, { passive: true })
  window.addEventListener('resize', onResize, { passive: true })
  window.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  if (!import.meta.client) return
  window.removeEventListener('scroll', onScroll)
  window.removeEventListener('resize', onResize)
  window.removeEventListener('keydown', onKeydown)
})

// 路由一变就收抽屉，否则点完链接菜单还挂在页面上
watch(() => route.fullPath, () => (menuOpen.value = false))
</script>

<template>
  <header
    class="sticky top-0 z-40 transition-colors duration-[var(--dur-base)]"
    :class="scrolled || menuOpen ? 'border-b border-hairline bg-canvas/85 backdrop-blur-md' : 'border-b border-transparent'"
  >
    <div class="mx-auto flex h-16 max-w-page items-center gap-6 px-5 sm:px-8">
      <NuxtLink to="/" class="shrink-0" aria-label="Research Copilot 首页">
        <MarketingLogo />
      </NuxtLink>

      <!-- 桌面导航 -->
      <nav class="ml-2 hidden items-center gap-1 lg:flex" aria-label="主导航">
        <NuxtLink
          v-for="item in nav"
          :key="item.to"
          :to="item.to"
          class="rounded-md px-3 py-1.5 text-[13.5px] text-ink-2 transition-colors duration-[var(--dur-fast)] hover:bg-hover hover:text-ink"
          :class="route.path === item.to ? 'text-ink' : ''"
        >
          {{ item.label }}
        </NuxtLink>
      </nav>

      <div class="ml-auto flex items-center gap-2">
        <NuxtLink
          to="/app"
          class="hidden items-center gap-1.5 rounded-lg bg-brand px-3.5 py-2 text-[13.5px] font-medium text-white transition-colors duration-[var(--dur-fast)] hover:bg-brand-ink sm:inline-flex"
        >
          进入工作台
          <PhArrowRight :size="14" weight="bold" />
        </NuxtLink>

        <button
          type="button"
          class="inline-flex h-9 w-9 items-center justify-center rounded-lg text-ink-2 transition-colors duration-[var(--dur-fast)] hover:bg-hover hover:text-ink lg:hidden"
          :aria-expanded="menuOpen"
          aria-controls="marketing-mobile-nav"
          :aria-label="menuOpen ? '关闭菜单' : '打开菜单'"
          @click="menuOpen = !menuOpen"
        >
          <PhX v-if="menuOpen" :size="19" />
          <PhList v-else :size="19" />
        </button>
      </div>
    </div>

    <!-- 移动端抽屉。用 v-show 而不是 v-if：保留 DOM 才能让 aria-controls 指向真实节点。 -->
    <div
      v-show="menuOpen"
      id="marketing-mobile-nav"
      class="border-t border-hairline bg-surface lg:hidden"
    >
      <nav class="mx-auto max-w-page px-5 py-3 sm:px-8" aria-label="移动导航">
        <NuxtLink
          v-for="item in nav"
          :key="item.to"
          :to="item.to"
          class="block rounded-lg px-3 py-3 text-[15px] text-ink-2 transition-colors duration-[var(--dur-fast)] hover:bg-hover hover:text-ink"
        >
          {{ item.label }}
        </NuxtLink>
        <NuxtLink
          to="/app"
          class="mt-2 flex items-center justify-center gap-1.5 rounded-lg bg-brand px-4 py-3 text-[15px] font-medium text-white"
        >
          进入工作台
          <PhArrowRight :size="15" weight="bold" />
        </NuxtLink>
      </nav>
    </div>
  </header>
</template>
