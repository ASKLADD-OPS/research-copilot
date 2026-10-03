<script setup lang="ts">
/**
 * PDF 阅读器：工具条 + 画布 + 缩略图轨。
 *
 * 壳与画布是分开的：`PdfCanvas` 只管"把这一页画出来、能划词、能画框"，
 * 翻页/缩放的**状态**放在这里 —— 双语对照页要从外面把 PDF 翻到某一段所在的页，
 * 状态就必须能被外部接管（`v-model:page`）。
 *
 * 与工作台的 `ReaderPanel` 的区别：那个绑了一组 Pinia store（选中的文献、划词、
 * 布局），只能在 `/app` 里用；这个是纯 props，谁都能挂。
 */
import {
  PhArrowSquareOut,
  PhArrowsHorizontal,
  PhCaretLeft,
  PhCaretRight,
  PhSquaresFour,
} from '@phosphor-icons/vue'
import type { HighlightRef, NormRect, ReaderTarget } from '~/types/workbench'

const props = withDefaults(
  defineProps<{
    url: string
    paperId: string
    /** 段落级高亮（双语对照） */
    highlights?: HighlightRef[]
    activeId?: string | null
    /** 跳转指令：给 bbox 就按坐标定位并滚到视野中间 */
    target?: ReaderTarget | null
  }>(),
  { highlights: () => [], activeId: null, target: null },
)

const emit = defineEmits<{
  'update:pageCount': [value: number]
  scroll: [position: { page: number; y: number; top: number }]
  select: [selection: { paperId: string; page: number; text: string; rects: NormRect[] }]
}>()

const page = defineModel<number>('page', { default: 1 })
const scale = defineModel<number>('scale', { default: 1.2 })

const pageCount = ref(0)
const fitWidth = ref(true)
const thumbs = ref(false)

// 换文档就回到第 1 页、回到适配宽度：沿用上一篇的缩放没有意义，字号与版心都不一样
watch(
  () => props.url,
  () => {
    page.value = 1
    scale.value = 1.2
    fitWidth.value = true
    pageCount.value = 0
  },
)

function goto(n: number) {
  page.value = Math.min(Math.max(1, n), pageCount.value || 1)
}

/** 页码框允许越界输入，改一次就收敛 —— 不收敛下一次翻页会从错误位置开始 */
function normalizePageInput(e: Event) {
  goto(Number((e.target as HTMLInputElement).value) || 1)
}

function zoom(delta: number) {
  fitWidth.value = false
  scale.value = Math.min(4, Math.max(0.25, +(scale.value + delta).toFixed(2)))
}
</script>

<template>
  <div class="flex h-full min-h-0 flex-col bg-canvas">
    <div class="flex h-9 shrink-0 items-center gap-1 border-b border-hairline bg-surface px-2">
      <button type="button" :class="iconBtnCls('sm')" :disabled="page <= 1" title="上一页" @click="goto(page - 1)">
        <PhCaretLeft :size="12" />
      </button>

      <div class="flex items-center gap-1" :class="MONO_CLS">
        <input
          :value="page"
          class="h-6 w-11 rounded-md border border-hairline-2 bg-surface text-center text-[11.5px] tabular-nums focus:border-brand focus:outline-none"
          type="number"
          min="1"
          :max="pageCount || 1"
          aria-label="页码"
          @change="normalizePageInput"
        />
        <span class="text-ink-4">/ {{ pageCount || '—' }}</span>
      </div>

      <button
        type="button"
        :class="iconBtnCls('sm')"
        :disabled="!pageCount || page >= pageCount"
        title="下一页"
        @click="goto(page + 1)"
      >
        <PhCaretRight :size="12" />
      </button>

      <span class="mx-0.5 h-4 w-px shrink-0 bg-hairline" />

      <button type="button" :class="iconBtnCls('sm')" title="缩小" @click="zoom(-0.2)">−</button>
      <span class="w-9 text-center text-[11.5px] tabular-nums text-ink-3">{{ Math.round(scale * 100) }}%</span>
      <button type="button" :class="iconBtnCls('sm')" title="放大" @click="zoom(0.2)">+</button>

      <button
        type="button"
        :class="[iconBtnCls('sm'), fitWidth ? 'bg-hover text-ink' : '']"
        title="适配宽度（面板拖宽时自动跟随）"
        @click="fitWidth = !fitWidth"
      >
        <PhArrowsHorizontal :size="12" />
      </button>

      <button
        type="button"
        :class="[iconBtnCls('sm'), thumbs ? 'bg-hover text-ink' : '']"
        title="缩略图"
        @click="thumbs = !thumbs"
      >
        <PhSquaresFour :size="12" />
      </button>

      <span class="flex-1" />

      <a :href="url" target="_blank" rel="noopener" :class="iconBtnCls('sm')" title="在新标签打开 / 下载">
        <PhArrowSquareOut :size="12" />
      </a>
    </div>

    <div class="min-h-0 flex-1">
      <!-- pdfjs 是纯浏览器库，SSR 阶段没有 canvas 与 TextDecoder 的完整实现 -->
      <ClientOnly>
        <PdfCanvas
          :url="url"
          :paper-id="paperId"
          v-model:page="page"
          v-model:scale="scale"
          :fit-width="fitWidth"
          :thumbs="thumbs"
          :highlights="highlights"
          :active-id="activeId"
          :target="target"
          @update:page-count="pageCount = $event"
          @scroll="emit('scroll', $event)"
          @select="emit('select', $event)"
        />
        <template #fallback>
          <div class="grid h-full place-items-center bg-sunken">
            <AppSpinner :size="18" class="text-ink-3" />
          </div>
        </template>
      </ClientOnly>
    </div>
  </div>
</template>
