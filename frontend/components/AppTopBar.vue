<script setup lang="ts">
/**
 * 顶栏。只放"全局只有一份"的东西：品牌、主视图切换、后端健康、布局复位。
 *
 * 刻意不放的动作：上传 PDF（在文献库面板里，那里才有拖拽区与解析链提示）、
 * 检索范围（在对话输入框旁，一句话问哪儿就显示哪儿）。
 * 顶栏每多一个控件，下面三栏能用的高度就少一分 —— 这是个工作台，不是门户。
 */
import { PhArrowsClockwise, PhFileText, PhGraph, PhHeartbeat, PhMagnifyingGlass, PhSparkle, PhArrowCounterClockwise } from '@phosphor-icons/vue'
import type { MainView } from '~/types/workbench'

const ui = useUiStore()
const selection = useSelectionStore()

const VIEWS = [
  { value: 'reader' as MainView, label: '阅读器', icon: PhFileText },
  { value: 'graph' as MainView, label: '引文图谱', icon: PhGraph },
]

const healthOpen = ref(false)
const healthTone = computed(() => {
  switch (ui.healthLight) {
    case 'ok':
      return 'bg-ok'
    case 'degraded':
      return 'bg-warn'
    case 'down':
      return 'bg-bad'
    default:
      return 'bg-ink-4'
  }
})

const healthText = computed(() => {
  switch (ui.healthLight) {
    case 'ok':
      return '正常'
    case 'degraded':
      return `降级 ${ui.unhealthy.length} 项`
    case 'down':
      return '不可达'
    default:
      return '未检查'
  }
})

/** 点外面关掉健康面板。用一次性监听而不是全局 listener，避免常驻开销。 */
function onDocClick() {
  healthOpen.value = false
  document.removeEventListener('click', onDocClick)
}

function toggleHealth() {
  healthOpen.value = !healthOpen.value
  if (healthOpen.value) {
    void ui.refreshHealth()
    // 下一帧才挂，否则这次点击本身就会把面板关掉
    setTimeout(() => document.addEventListener('click', onDocClick), 0)
  }
}

onBeforeUnmount(() => {
  if (import.meta.client) document.removeEventListener('click', onDocClick)
})
</script>

<template>
  <header class="flex h-11 shrink-0 items-center gap-2 border-b border-hairline bg-surface px-3">
    <div class="flex shrink-0 items-center gap-2">
      <span class="grid size-5 place-items-center rounded-[6px] bg-brand text-white">
        <PhSparkle :size="12" weight="fill" />
      </span>
      <span class="text-[13px] font-semibold tracking-tight text-ink">Research Copilot</span>
    </div>

    <span class="mx-1 h-4 w-px shrink-0 bg-hairline" />

    <Segmented v-model="ui.layout.mainView" :options="VIEWS" size="sm" aria-label="主视图切换" />

    <!-- 主题探索。独立页面而不是第四个主视图：它会占满整屏、有自己的开始与结束，
         塞进三栏工作台会把"看着原文提问"的空间挤没。 -->
    <NuxtLink
      to="/app/tools"
      class="inline-flex h-6.5 shrink-0 items-center gap-1.5 rounded-md px-2 text-[11.5px] text-ink-3 transition-colors hover:bg-hover hover:text-ink"
      title="给一个主题，让 Agent 自动检索、下载并推荐"
    >
      <PhMagnifyingGlass :size="12" />
      主题探索
    </NuxtLink>

    <span class="min-w-0 flex-1" />

    <!-- 当前选中的文献：三栏共用一个"现在在看什么"，顶栏是它最该露出的地方 -->
    <div v-if="selection.active" class="flex min-w-0 items-center gap-1.5 text-[12px]">
      <span class="text-ink-4">当前</span>
      <span class="truncate font-medium text-ink-2">{{ selection.active.title }}</span>
    </div>

    <span class="min-w-0 flex-1" />

    <span
      v-if="ui.activeTasks.length"
      class="inline-flex shrink-0 items-center gap-1.5 text-2xs text-ink-3"
      :title="`${ui.activeTasks.length} 个后台任务进行中`"
    >
      <AppSpinner :size="11" class="text-brand" />
      {{ ui.activeTasks.length }} 个任务
    </span>

    <!-- 健康灯。点开才展开细节 —— 平时它只是个点。 -->
    <div class="relative shrink-0">
      <button
        type="button"
        class="inline-flex h-6.5 items-center gap-1.5 rounded-md px-2 text-[11.5px] text-ink-3 transition-colors hover:bg-hover hover:text-ink"
        :title="`后端：${healthText}`"
        @click.stop="toggleHealth"
      >
        <span class="size-1.5 rounded-full" :class="healthTone" />
        {{ healthText }}
      </button>

      <div
        v-if="healthOpen"
        class="absolute top-8 right-0 z-30 w-72 overflow-hidden rounded-lg border border-hairline bg-surface shadow-md"
        @click.stop
      >
        <div class="flex items-center gap-2 border-b border-hairline px-3 py-2">
          <PhHeartbeat :size="13" class="text-ink-3" />
          <span class="text-[12px] font-semibold">后端组件</span>
          <span class="flex-1" />
          <button
            type="button"
            class="grid size-5 place-items-center rounded-sm text-ink-4 hover:bg-hover hover:text-ink"
            title="重新检查"
            @click="ui.refreshHealth()"
          >
            <PhArrowsClockwise :size="11" :class="{ 'animate-spin': ui.checkingHealth }" />
          </button>
        </div>
        <ul v-if="ui.health" class="divide-y divide-hairline">
          <li
            v-for="c in ui.health.components"
            :key="c.name"
            class="flex items-center gap-2 px-3 py-1.5 text-[11.5px]"
          >
            <span class="size-1.5 shrink-0 rounded-full" :class="c.ok ? 'bg-ok' : 'bg-bad'" />
            <span class="font-medium text-ink-2">{{ c.name }}</span>
            <span class="flex-1" />
            <span class="text-ink-4">{{ c.latency_ms != null ? `${c.latency_ms}ms` : '—' }}</span>
          </li>
        </ul>
        <p v-else class="px-3 py-2 text-[11.5px] text-ink-3">
          {{ ui.healthError || '正在检查…' }}
        </p>
        <p v-if="ui.health" class="border-t border-hairline px-3 py-1.5 text-2xs text-ink-4">
          {{ ui.health.version }} · {{ ui.health.env }}
        </p>
      </div>
    </div>

    <button
      type="button"
      class="grid size-6.5 shrink-0 place-items-center rounded-md text-ink-3 transition-colors hover:bg-hover hover:text-ink"
      title="恢复默认布局"
      @click="ui.resetLayout()"
    >
      <PhArrowCounterClockwise :size="13" />
    </button>
  </header>
</template>
