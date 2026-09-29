<script setup lang="ts">
/**
 * 文献列表项。
 *
 * 直接读 store 而不是层层 emit：它是文献库面板的叶子，改状态的地方只有
 * library/selection 两个 store，中间再插一层事件转发没有收益。
 * 唯一例外是"编辑" —— 弹窗归父组件管，所以那条走 emit。
 */
import {
  PhArrowsClockwise,
  PhCaretRight,
  PhFilePdf,
  PhPencilSimple,
  PhSelectionAll,
  PhTrash,
} from '@phosphor-icons/vue'
import type { Paper } from '~/types/api'
import { STATUS_LABEL, TRANSIENT_STATUS } from '~/stores/library'

const props = defineProps<{ paper: Paper; index: number }>()
const emit = defineEmits<{ edit: [paper: Paper] }>()

const library = useLibraryStore()
const selection = useSelectionStore()

const busy = computed(() => TRANSIENT_STATUS.includes(props.paper.status))
const active = computed(() => selection.isActive(props.paper.id))
const inScope = computed(() => library.inScope(props.paper.id))

const authorLine = computed(() => {
  const names = (props.paper.authors ?? []).map((a) => a.name).filter(Boolean)
  if (!names.length) return ''
  return names.length > 2 ? `${names[0]} 等 ${names.length} 人` : names.join('、')
})

const metaLine = computed(() =>
  [authorLine.value, props.paper.year, props.paper.venue].filter(Boolean).join(' · '),
)

async function reindex(e: Event) {
  e.stopPropagation()
  await library.reindex(props.paper.id)
}

async function remove(e: Event) {
  e.stopPropagation()
  if (!confirm(`删除《${props.paper.title}》？该文献的分块与引用关系会一并移除。`)) return
  await library.remove(props.paper.id)
  if (selection.isActive(props.paper.id)) selection.clear()
}
</script>

<template>
  <div
    class="group relative cursor-pointer border-b border-hairline px-2.5 py-2 transition-colors"
    :class="active ? 'bg-brand-soft' : 'hover:bg-hover'"
    role="button"
    tabindex="0"
    :aria-current="active"
    @click="selection.select(paper.id)"
    @keydown.enter.prevent="selection.select(paper.id)"
  >
    <!-- 选中态只靠左侧 2px 色条 + 淡底，不用边框：列表里加边框会让每行高度抖动 -->
    <span
      v-if="active"
      class="absolute inset-y-0 left-0 w-[2px] bg-brand"
      aria-hidden="true"
    />

    <div class="flex items-start gap-2">
      <PhFilePdf
        :size="13"
        class="mt-0.5 shrink-0"
        :class="paper.status === 'failed' ? 'text-bad' : 'text-ink-4'"
      />
      <div class="min-w-0 flex-1">
        <p class="line-clamp-2 text-[12.5px] leading-snug font-medium text-ink">
          {{ paper.title || '（未命名）' }}
        </p>
        <p v-if="metaLine" class="mt-0.5 truncate text-2xs text-ink-4">{{ metaLine }}</p>
      </div>

      <!-- 行内动作：默认藏起来，hover 或选中才出。列表要的是"扫一遍标题"，不是按钮墙 -->
      <div
        class="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100"
        :class="{ 'opacity-100': active }"
      >
        <button
          type="button"
          class="grid size-5 place-items-center rounded-sm text-ink-4 hover:bg-active hover:text-ink"
          :title="inScope ? '移出检索范围' : '加入检索范围'"
          @click.stop="library.toggleScope(paper.id)"
        >
          <PhSelectionAll :size="11" :class="{ 'text-brand': inScope }" />
        </button>
        <button
          type="button"
          class="grid size-5 place-items-center rounded-sm text-ink-4 hover:bg-active hover:text-ink"
          title="重新解析"
          @click="reindex"
        >
          <PhArrowsClockwise :size="11" />
        </button>
        <button
          type="button"
          class="grid size-5 place-items-center rounded-sm text-ink-4 hover:bg-active hover:text-ink"
          title="编辑元数据"
          @click.stop="emit('edit', paper)"
        >
          <PhPencilSimple :size="11" />
        </button>
        <button
          type="button"
          class="grid size-5 place-items-center rounded-sm text-ink-4 hover:bg-bad-soft hover:text-bad"
          title="删除"
          @click="remove"
        >
          <PhTrash :size="11" />
        </button>
      </div>
    </div>

    <div class="mt-1.5 flex items-center gap-1.5">
      <span :class="pillCls(statusTone(paper.status))">
        <AppSpinner v-if="busy" :size="9" />
        {{ STATUS_LABEL[paper.status] }}
      </span>
      <span v-if="paper.num_chunks" class="text-2xs text-ink-4">{{ paper.num_chunks }} 块</span>
      <!-- MinerU 降级是静默的，所以实际生效的解析器必须露出来 -->
      <span v-if="paper.parser" class="text-2xs text-ink-4">{{ paper.parser }}</span>
      <span v-if="paper.page_count" class="text-2xs text-ink-4">{{ paper.page_count }} 页</span>
      <span class="min-w-0 flex-1" />
      <PhCaretRight v-if="active" :size="10" class="shrink-0 text-brand" />
    </div>

    <p v-if="paper.error" class="mt-1 line-clamp-2 rounded-sm bg-bad-soft px-1.5 py-1 text-2xs text-bad">
      {{ paper.error }}
    </p>
  </div>
</template>
