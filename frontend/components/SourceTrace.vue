<script setup lang="ts">
/**
 * 溯源条目 —— 「答案里的这句话 ← 哪一块原文支撑」。
 *
 * 与 `CitationList` 的分工：那个按 marker 列引用（写作面板也复用），这个按**答案片段**列，
 * 每条带规格要求的 `[paper_id:page:chunk_id]` 定位徽章、hover 展开原文片段、
 * 点击直达 PDF 并把 bbox 框出来。
 *
 * 它是抗幻觉机制的核对入口：用户不该相信"有据率 87%"这个数字，而该能一键去看原处——
 * 数字可以被调参调到好看，原处不会。
 */
import { PhArrowSquareOut, PhCheckCircle, PhFileText, PhWarningCircle } from '@phosphor-icons/vue'
import { pageStart, type Citation } from '~/types/api'
import { rectsFromBbox } from '~/utils/ui'

const props = withDefaults(defineProps<{ citations: Citation[]; compact?: boolean }>(), {
  compact: false,
})

const selection = useSelectionStore()
const ui = useUiStore()

/** 定位徽章文本：`[paper_id:page:chunk_id]`，缺哪个用 `-` 占位。 */
function badge(c: Citation) {
  return `[${c.paper_id ?? '-'}:${pageStart(c) ?? '-'}:${c.chunk_id ?? '-'}]`
}

/** 没有 paper_id 就跳不了 PDF —— 这种条目降级成不可点，而不是点了没反应。 */
function canOpen(c: Citation) {
  return Boolean(c.paper_id)
}

function open(c: Citation) {
  if (!canOpen(c)) return
  // 换到阅读器再跳：点引用的意图就是"看原文"，留在图谱页等于没反应
  ui.setMainView('reader')
  void selection.openAt(String(c.paper_id), pageStart(c) ?? 1, c.quote, rectsFromBbox(c.bbox))
}
</script>

<template>
  <ul v-if="props.citations.length" class="space-y-1">
    <li v-for="c in props.citations" :key="`${c.marker}-${c.chunk_id}`" class="relative">
      <button
        type="button"
        class="group/cite block w-full rounded-md px-2 py-1.5 text-left transition-colors"
        :class="canOpen(c) ? 'hover:bg-hover' : 'cursor-default opacity-70'"
        @click="open(c)"
      >
        <span class="flex items-center gap-1.5">
          <span v-if="c.marker != null" class="cite-mark shrink-0">{{ c.marker }}</span>
          <span class="shrink-0 rounded-xs bg-sunken px-1 py-px font-mono text-[10px] text-ink-3">
            {{ badge(c) }}
          </span>
          <PhFileText :size="11" class="shrink-0 text-ink-4" />
          <span class="min-w-0 flex-1 truncate text-[11.5px] font-medium text-ink-2">
            {{ c.title || c.paper_id }}
          </span>
          <span v-if="c.confidence" class="shrink-0 text-2xs tabular-nums text-ink-4">
            {{ (c.confidence * 100).toFixed(0) }}%
          </span>
          <PhCheckCircle
            v-if="c.verified"
            :size="11"
            class="shrink-0 text-ok"
            weight="fill"
            title="已通过 NLI 蕴含校验"
          />
          <PhWarningCircle v-else :size="11" class="shrink-0 text-warn" title="未通过蕴含校验，请自行核对" />
          <PhArrowSquareOut v-if="canOpen(c)" :size="10" class="shrink-0 text-ink-4" />
        </span>

        <!-- 答案里被这条证据支撑的那句话 -->
        <span
          v-if="!props.compact && c.answer_span"
          class="mt-1 line-clamp-2 block border-l border-hairline pl-2 text-2xs leading-relaxed text-ink-3"
        >
          {{ c.answer_span }}
        </span>
      </button>

      <!--
        Hover 展开原文片段。用 `pointer-events-none` 的绝对定位浮层而不是 `title`：
        原生 tooltip 是浏览器画的，样式不受控、还只在停够一秒后才出现。
        向上展开 —— 列表最后一条向下弹会被容器裁掉。
      -->
      <span
        class="pointer-events-none absolute bottom-full left-2 right-2 z-20 mb-1 hidden max-h-48 overflow-hidden rounded-md border border-hairline bg-surface p-2 text-2xs leading-relaxed text-ink-2 shadow-lg group-hover/cite:block"
      >
        <span class="mb-1 block font-mono text-[10px] text-ink-4">{{ badge(c) }}</span>
        {{ c.quote || '（没有原文片段）' }}
      </span>
    </li>
  </ul>
</template>
