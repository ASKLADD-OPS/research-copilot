<script setup lang="ts">
/**
 * 检视面板：当前选中文献的元数据、本页分块、划词记录。
 *
 * 放右栏的原因：这三样都是"围绕当前这一篇"的细节，而右栏的另外两个标签
 * （对话 / 写作）也是围绕当前范围展开的 —— 它们共享同一个上下文，
 * 拆到不同页面就得靠用户自己记住上下文是什么。
 */
import {
  PhArrowSquareOut,
  PhArrowsClockwise,
  PhInfo,
  PhSelectionAll,
  PhQuotes,
} from '@phosphor-icons/vue'
import { STATUS_LABEL } from '~/stores/library'

const selection = useSelectionStore()
const library = useLibraryStore()
const chat = useChatStore()
const ui = useUiStore()

const showAbstract = ref(false)

const paper = computed(() => selection.active)

const authorLine = computed(() =>
  (paper.value?.authors ?? [])
    .map((a) => (a.affiliation ? `${a.name}（${a.affiliation}）` : a.name))
    .filter(Boolean)
    .join('、'),
)

const metaRows = computed(() => {
  const p = paper.value
  if (!p) return [] as { k: string; v: string }[]
  return [
    { k: '年份', v: p.year ? String(p.year) : '—' },
    { k: '来源', v: p.venue || '—' },
    { k: 'DOI', v: p.doi || '—' },
    { k: 'arXiv', v: p.arxiv_id || '—' },
    { k: '页数', v: p.page_count ? `${p.page_count} 页` : '—' },
    { k: '分块', v: p.num_chunks ? `${p.num_chunks} 块` : '—' },
    { k: '解析器', v: p.parser || '—' },
    { k: '文件', v: formatBytes(p.file_size) },
  ]
})

function askSelection(index: number) {
  const sel = selection.selections[index]
  if (!sel) return
  chat.attachQuote(sel, paper.value?.title ?? '当前文献')
  ui.setWorkTab('chat')
}

function jumpSelection(index: number) {
  const sel = selection.selections[index]
  if (!sel) return
  ui.setMainView('reader')
  selection.requestPage(sel.page, sel.text)
}
</script>

<template>
  <div v-if="!paper" class="grid h-full place-items-center px-4">
    <div :class="EMPTY_CLS">
      <PhInfo :size="20" class="mb-1.5 text-ink-4" />
      <strong class="text-[12.5px] text-ink-2">还没有选中文献</strong>
      <span class="max-w-56 text-2xs leading-relaxed text-ink-4">
        在左侧文献库里点一篇，这里会出现它的元数据、分块与划词记录。
      </span>
    </div>
  </div>

  <div v-else class="h-full overflow-y-auto scroll-slim">
    <!-- 标题与动作 -->
    <div class="border-b border-hairline px-2.5 py-2">
      <h3 class="text-[13px] leading-snug font-semibold text-ink">{{ paper.title }}</h3>
      <p v-if="authorLine" class="mt-0.5 text-2xs leading-relaxed text-ink-3">{{ authorLine }}</p>

      <div class="mt-1.5 flex flex-wrap items-center gap-1.5">
        <span :class="pillCls(statusTone(paper.status))">{{ STATUS_LABEL[paper.status] }}</span>
        <span v-for="t in paper.tags" :key="t" :class="pillCls('brand')">{{ t }}</span>
      </div>

      <div class="mt-2 flex flex-wrap gap-1.5">
        <a
          :href="library.pdfUrl(paper.id)"
          target="_blank"
          rel="noopener"
          :class="btnCls('default', { size: 'sm' })"
        >
          <PhArrowSquareOut :size="10" />
          打开 PDF
        </a>
        <button
          type="button"
          :class="btnCls('default', { size: 'sm' })"
          :title="library.inScope(paper.id) ? '从检索范围移除' : '加入检索范围'"
          @click="library.toggleScope(paper.id)"
        >
          <PhSelectionAll :size="10" />
          {{ library.inScope(paper.id) ? '移出范围' : '加入范围' }}
        </button>
        <button
          type="button"
          :class="btnCls('ghost', { size: 'sm' })"
          @click="library.reindex(paper.id)"
        >
          <PhArrowsClockwise :size="10" />
          重新解析
        </button>
      </div>
    </div>

    <!-- 元数据 -->
    <dl class="grid grid-cols-2 gap-x-3 gap-y-1 border-b border-hairline px-2.5 py-2 text-2xs">
      <div v-for="r in metaRows" :key="r.k" class="flex min-w-0 justify-between gap-2">
        <dt class="shrink-0 text-ink-4">{{ r.k }}</dt>
        <dd class="truncate text-ink-2" :title="r.v">{{ r.v }}</dd>
      </div>
    </dl>

    <!-- 摘要 -->
    <div v-if="paper.abstract" class="border-b border-hairline px-2.5 py-2">
      <button
        type="button"
        class="flex w-full items-center gap-1.5 text-2xs font-medium text-ink-3"
        @click="showAbstract = !showAbstract"
      >
        <PhInfo :size="11" />
        摘要
        <span class="flex-1 text-left font-normal text-ink-4">
          {{ showAbstract ? '' : '点击展开' }}
        </span>
      </button>
      <p
        v-if="showAbstract"
        class="mt-1.5 text-2xs leading-relaxed text-ink-2"
      >
        {{ paper.abstract }}
      </p>
    </div>

    <!-- 本页分块 -->
    <div class="border-b border-hairline px-2.5 py-2">
      <p class="mb-1 flex items-center gap-1.5 text-2xs font-medium text-ink-3">
        本页分块
        <span :class="pillCls('default')">第 {{ selection.page }} 页</span>
        <span class="text-ink-4">{{ selection.pageChunks.length }} 块</span>
      </p>
      <ul v-if="selection.pageChunks.length" class="space-y-1">
        <li
          v-for="c in selection.pageChunks"
          :key="c.id"
          class="rounded-md bg-sunken px-2 py-1.5 text-2xs leading-relaxed text-ink-2"
        >
          <span v-if="c.section_name" class="mb-0.5 block text-ink-4">{{ c.section_name }}</span>
          <span class="line-clamp-3">{{ c.content }}</span>
        </li>
      </ul>
      <p v-else class="text-2xs text-ink-4">这一页没有落在任何分块里。</p>
    </div>

    <!-- 划词记录 -->
    <div class="px-2.5 py-2">
      <p class="mb-1 flex items-center gap-1.5 text-2xs font-medium text-ink-3">
        <PhQuotes :size="11" />
        划词记录
        <span class="text-ink-4">{{ selection.selections.length }}</span>
        <span class="flex-1" />
        <button
          v-if="selection.selections.length"
          type="button"
          class="text-ink-4 hover:text-ink"
          @click="selection.clearSelections()"
        >
          清空
        </button>
      </p>

      <ul v-if="selection.selections.length" class="space-y-1">
        <li
          v-for="(s, i) in [...selection.selections].reverse()"
          :key="s.createdAt"
          class="group rounded-md border border-hairline px-2 py-1.5"
        >
          <p class="line-clamp-2 text-2xs leading-relaxed text-ink-2">{{ s.text }}</p>
          <p class="mt-1 flex items-center gap-1.5">
            <span class="text-2xs text-ink-4">p.{{ s.page }}</span>
            <span class="flex-1" />
            <button
              type="button"
              class="text-2xs text-ink-4 hover:text-ink"
              @click="jumpSelection(selection.selections.length - 1 - i)"
            >
              定位
            </button>
            <button
              type="button"
              class="text-2xs text-brand-ink hover:opacity-70"
              @click="askSelection(selection.selections.length - 1 - i)"
            >
              追问
            </button>
          </p>
        </li>
      </ul>
      <p v-else class="text-2xs leading-relaxed text-ink-4">
        在阅读器里用鼠标选中文字，就会在这里留下锚点。
      </p>
    </div>
  </div>
</template>
