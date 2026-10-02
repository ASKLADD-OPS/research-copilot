<script setup lang="ts">
/**
 * 引用列表。
 *
 * 每一条都能点：跳到 PDF 对应页并把原句高亮出来。这是抗幻觉机制在界面上的落点 ——
 * 用户不该"相信"有据率这个数字，而该能一键去核对。
 */
import { PhArrowSquareOut, PhCheckCircle, PhFileText, PhWarningCircle } from '@phosphor-icons/vue'
import { pageStart, type Citation } from '~/types/api'
import { rectsFromBbox } from '~/utils/ui'

const props = withDefaults(defineProps<{ citations: Citation[]; compact?: boolean }>(), {
  compact: false,
})

const selection = useSelectionStore()
const ui = useUiStore()

function open(c: Citation) {
  // 换到阅读器再跳：用户点引用的意图就是"看原文"，留在图谱页等于没反应
  ui.setMainView('reader')
  // 有 bbox 就按坐标框（精确），没有才退回按 quote 文字找
  void selection.openAt(c.paper_id, pageStart(c) ?? 1, c.quote, rectsFromBbox(c.bbox))
}

const pageLabel = (c: Citation) => {
  const start = pageStart(c)
  if (start == null) return ''
  return c.page_end && c.page_end !== start ? `p.${start}–${c.page_end}` : `p.${start}`
}
</script>

<template>
  <ul v-if="props.citations.length" class="space-y-1">
    <li v-for="c in props.citations" :key="`${c.marker}-${c.chunk_id}`">
      <button
        type="button"
        class="w-full rounded-md px-2 py-1.5 text-left transition-colors hover:bg-hover"
        :title="c.quote"
        @click="open(c)"
      >
        <span class="flex items-center gap-1.5">
          <span v-if="c.marker != null" class="cite-mark shrink-0">{{ c.marker }}</span>
          <PhFileText :size="11" class="shrink-0 text-ink-4" />
          <span class="min-w-0 flex-1 truncate text-[11.5px] font-medium text-ink-2">
            {{ c.title || c.paper_id }}
          </span>
          <span v-if="pageLabel(c)" class="shrink-0 text-2xs tabular-nums text-ink-4">
            {{ pageLabel(c) }}
          </span>
          <PhCheckCircle
            v-if="c.verified"
            :size="11"
            class="shrink-0 text-ok"
            weight="fill"
            title="已通过 NLI 蕴含校验"
          />
          <PhWarningCircle
            v-else
            :size="11"
            class="shrink-0 text-warn"
            title="未通过蕴含校验，请自行核对"
          />
          <PhArrowSquareOut :size="10" class="shrink-0 text-ink-4" />
        </span>
        <span
          v-if="!props.compact"
          class="mt-1 line-clamp-2 block border-l border-hairline pl-2 text-2xs leading-relaxed text-ink-4"
        >
          {{ c.quote }}
        </span>
      </button>
    </li>
  </ul>
</template>
