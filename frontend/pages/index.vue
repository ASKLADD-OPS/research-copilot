<script setup lang="ts">
/**
 * 工作台。整个应用只有这一个页面 —— 文献库 / 阅读器（或引文图谱）/ 右栏工作区
 * 三列并排，中间两条分割线可拖拽、可折叠。
 *
 * 为什么做成单页而不是五个路由：这个产品的核心动作是"看着原文提问"，
 * 跨页跳转会把原文、选中范围、上下文全部丢掉。ponder 讲的"一站式空间"
 * 说的就是这件事。
 */
import { PANEL_BOUNDS } from '~/stores/ui'

const ui = useUiStore()
const library = useLibraryStore()
const selection = useSelectionStore()

// 折叠时留一条 46px 的竖条，而不是 0 —— 完全消失的面板没法再点开
const COLLAPSED_W = 46

const libraryWidth = computed(() =>
  ui.layout.library.collapsed ? COLLAPSED_W : ui.layout.library.size,
)
const workWidth = computed(() => (ui.layout.work.collapsed ? COLLAPSED_W : ui.layout.work.size))

/**
 * 全局快捷键。
 * 输入框里敲字时一律不响应 —— 否则在搜索框里打 cmd+b 会把文献库收起来。
 */
function onKeydown(e: KeyboardEvent) {
  const target = e.target as HTMLElement | null
  if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) {
    return
  }
  if (!(e.metaKey || e.ctrlKey)) return
  switch (e.key.toLowerCase()) {
    case 'b':
      e.preventDefault()
      ui.togglePanel('library')
      break
    case 'j':
      e.preventDefault()
      ui.togglePanel('work')
      break
    case '1':
      e.preventDefault()
      ui.setMainView('reader')
      break
    case '2':
      e.preventDefault()
      ui.setMainView('graph')
      break
  }
}

onMounted(() => {
  ui.restoreLayout()
  window.addEventListener('keydown', onKeydown)
  void ui.refreshHealth()
  void ui.loadTasks()
})

onBeforeUnmount(() => {
  if (import.meta.client) window.removeEventListener('keydown', onKeydown)
  library.stopAllPolling()
})
</script>

<template>
  <div class="flex h-screen flex-col overflow-hidden bg-canvas">
    <AppTopBar />

    <div class="flex min-h-0 flex-1">
      <!-- 左：文献库 -->
      <div
        class="shrink-0 overflow-hidden border-r border-hairline bg-surface"
        :style="{ width: `${libraryWidth}px` }"
      >
        <LibraryPanel :collapsed="ui.layout.library.collapsed" @toggle="ui.togglePanel('library')" />
      </div>

      <SplitHandle
        :model-value="ui.layout.library.size"
        :min="PANEL_BOUNDS.library[0]"
        :max="PANEL_BOUNDS.library[1]"
        side="left"
        :collapsed="ui.layout.library.collapsed"
        label="文献库宽度"
        @update:model-value="ui.setPanelSize('library', $event)"
        @toggle="ui.togglePanel('library')"
      />

      <!-- 中：阅读器 / 引文图谱。min-w-0 是必须的，否则 flex 子项不会收缩，
           左栏一拖宽就会把右侧整块挤出屏幕 -->
      <main class="min-w-0 flex-1 bg-surface">
        <ReaderPanel v-if="ui.layout.mainView === 'reader'" />
        <GraphPanel v-else />
      </main>

      <SplitHandle
        :model-value="ui.layout.work.size"
        :min="PANEL_BOUNDS.work[0]"
        :max="PANEL_BOUNDS.work[1]"
        side="right"
        :collapsed="ui.layout.work.collapsed"
        label="右栏宽度"
        @update:model-value="ui.setPanelSize('work', $event)"
        @toggle="ui.togglePanel('work')"
      />

      <!-- 右：对话 / 检视 / 写作 / 工具 -->
      <div
        class="shrink-0 overflow-hidden border-l border-hairline bg-surface"
        :style="{ width: `${workWidth}px` }"
      >
        <WorkPanel :collapsed="ui.layout.work.collapsed" @toggle="ui.togglePanel('work')" />
      </div>
    </div>

    <AppStatusBar />
  </div>

  <!-- 划词后给屏幕阅读器一个提示；视觉上的反馈在阅读器底部的动作条上 -->
  <p class="sr-only" aria-live="polite">
    {{ selection.latestSelection ? `已选中：${selection.latestSelection.text.slice(0, 40)}` : '' }}
  </p>
</template>
