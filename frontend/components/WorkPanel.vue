<script setup lang="ts">
/**
 * 右栏工作区：对话 / 检视 / 写作 / 工具 四个标签共用一个面板。
 *
 * 为什么不做成四个页面：这四件事共享同一个上下文（当前选中文献 + 检索范围）。
 * 拆成页面之后，用户在"写作"里想看一眼原文，就得先跳到阅读器、再跳回来，
 * 而右栏本来就是"不打断思路"的那一侧。
 */
import { PhChatCircleDots, PhInfo, PhPencilLine, PhWrench } from '@phosphor-icons/vue'
import type { WorkTab } from '~/types/workbench'

defineProps<{ collapsed: boolean }>()
const emit = defineEmits<{ toggle: [] }>()

const ui = useUiStore()

const TABS: ReadonlyArray<{ value: WorkTab; label: string; icon: typeof PhInfo }> = [
  { value: 'chat', label: '对话', icon: PhChatCircleDots },
  { value: 'inspect', label: '检视', icon: PhInfo },
  { value: 'writing', label: '写作', icon: PhPencilLine },
  { value: 'tools', label: '工具', icon: PhWrench },
]

const active = computed(() => TABS.find((t) => t.value === ui.layout.workTab) ?? TABS[0])

/** 折叠态的竖排文字只取当前标签名，四个字塞不下 */
const collapsedTitle = computed(() => active.value.label)
</script>

<template>
  <AppPanel
    :title="collapsedTitle"
    :icon="active.icon"
    side="right"
    :collapsed="collapsed"
    @toggle="emit('toggle')"
  >
    <template #title>
      <Segmented
        :model-value="ui.layout.workTab"
        :options="TABS"
        size="sm"
        aria-label="右栏标签"
        @update:model-value="ui.setWorkTab($event)"
      />
    </template>

    <ChatPanel v-if="ui.layout.workTab === 'chat'" />
    <InspectPanel v-else-if="ui.layout.workTab === 'inspect'" />
    <WritingPanel v-else-if="ui.layout.workTab === 'writing'" />
    <ToolsPanel v-else />
  </AppPanel>
</template>
