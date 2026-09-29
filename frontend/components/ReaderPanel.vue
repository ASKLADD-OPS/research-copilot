<script setup lang="ts">
/**
 * 阅读器面板：工具条 + PDF 画布 + 划词动作条。
 *
 * 页号与缩放由这里持有并下发给 PdfCanvas —— 工具条和画布必须看同一份状态，
 * 而"跳转指令"走 selection store 的 target（带 nonce），因为触发方在右栏的引用列表里，
 * 中间隔着两层组件，用 props 接力会把整条链路都写脏。
 */
import {
  PhArrowSquareOut,
  PhCaretLeft,
  PhCaretRight,
  PhFileText,
  PhQuotes,
  PhSelectionAll,
  PhArrowsHorizontal,
  PhDownloadSimple,
} from '@phosphor-icons/vue'

const selection = useSelectionStore()
const library = useLibraryStore()
const chat = useChatStore()
const ui = useUiStore()

const page = ref(1)
const scale = ref(1.2)
const pageCount = ref(0)
const fitWidth = ref(true)

const pdfUrl = computed(() => (selection.activeId ? library.pdfUrl(selection.activeId) : ''))

// 换文档就回到第 1 页、回到适配宽度：沿用上一篇的缩放没有意义，
// 两篇论文的字号与版心宽度根本不一样
watch(
  () => pdfUrl.value,
  () => {
    page.value = 1
    scale.value = 1.2
    fitWidth.value = true
  },
)

function onPage(v: number) {
  page.value = v
  selection.setPage(v)
}

function goto(n: number) {
  const next = Math.min(Math.max(1, n), pageCount.value || 1)
  if (next !== page.value) onPage(next)
}

/** 页码框允许越界输入，失焦时收敛 —— 不收敛下一次翻页会从错误位置开始 */
function normalizePageInput(e: Event) {
  const input = e.target as HTMLInputElement
  goto(Number(input.value) || 1)
}

function zoom(delta: number) {
  fitWidth.value = false
  scale.value = Math.min(4, Math.max(0.25, +(scale.value + delta).toFixed(2)))
}

// ---------------- 划词动作 ----------------
const last = computed(() => selection.latestSelection)

function askAboutSelection() {
  const sel = last.value
  if (!sel) return
  chat.attachQuote(sel, selection.active?.title ?? '当前文献')
  // 追问一定发生在对话里，顺手把右栏切到对话并展开
  ui.setWorkTab('chat')
  ui.setPanelCollapsed('work', false)
}

function selectWholePage() {
  // 页面级划词退化成"没有锚点"的全库提问：交给用户手动圈定，比猜他要哪一段强
  selection.clearSelections()
}

async function copySelection() {
  if (!last.value) return
  try {
    await navigator.clipboard.writeText(last.value.text)
  } catch {
    /* 非安全上下文（http）下剪贴板不可用，静默即可 */
  }
}
</script>

<template>
  <AppPanel
    title="阅读器"
    :icon="PhFileText"
    side="left"
    :collapsible="false"
    :meta="selection.active ? `${page} / ${pageCount || '—'}` : ''"
  >
    <template #actions>
      <template v-if="selection.active">
        <button
          type="button"
          :class="iconBtnCls('sm')"
          :disabled="page <= 1"
          title="上一页"
          @click="goto(page - 1)"
        >
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
          :disabled="pageCount === 0 || page >= pageCount"
          title="下一页"
          @click="goto(page + 1)"
        >
          <PhCaretRight :size="12" />
        </button>

        <span class="mx-0.5 h-4 w-px bg-hairline" />

        <button type="button" :class="iconBtnCls('sm')" title="缩小" @click="zoom(-0.2)">−</button>
        <span class="w-9 text-center text-[11.5px] tabular-nums text-ink-3">
          {{ Math.round(scale * 100) }}%
        </span>
        <button type="button" :class="iconBtnCls('sm')" title="放大" @click="zoom(0.2)">+</button>

        <button
          type="button"
          :class="[iconBtnCls('sm'), fitWidth ? 'bg-hover text-ink' : '']"
          title="适配宽度（面板拖宽时自动跟随）"
          @click="fitWidth = !fitWidth"
        >
          <PhArrowsHorizontal :size="12" />
        </button>

        <span class="mx-0.5 h-4 w-px bg-hairline" />

        <a
          :href="pdfUrl"
          target="_blank"
          rel="noopener"
          :class="iconBtnCls('sm')"
          title="在新标签打开 / 下载"
        >
          <PhArrowSquareOut :size="12" />
        </a>
      </template>
    </template>

    <div class="relative h-full">
      <template v-if="selection.active">
        <div class="flex h-full flex-col">
          <div class="min-h-0 flex-1">
            <!-- pdfjs 是纯浏览器组件，SSR 阶段没有 canvas 与 TextDecoder 的完整实现 -->
            <ClientOnly>
              <PdfCanvas
                :url="pdfUrl"
                :paper-id="selection.activeId!"
                :page="page"
                :scale="scale"
                :fit-width="fitWidth"
                :target="selection.target"
                @update:page="onPage"
                @update:scale="scale = $event"
                @update:page-count="pageCount = $event"
                @select="selection.pushSelection($event)"
              />
              <template #fallback>
                <div class="grid h-full place-items-center bg-sunken">
                  <AppSpinner :size="18" class="text-ink-3" />
                </div>
              </template>
            </ClientOnly>
          </div>

          <!-- 划词动作条：只在这一刻出现，因为"下一步干什么"完全取决于刚选中的是什么 -->
          <div
            v-if="last"
            class="shrink-0 border-t border-hairline bg-surface px-2.5 py-2"
          >
            <div class="flex items-start gap-2">
              <PhQuotes :size="13" class="mt-0.5 shrink-0 text-brand" />
              <p class="line-clamp-2 flex-1 text-2xs leading-relaxed text-ink-2">
                {{ last.text }}
              </p>
              <div class="flex shrink-0 items-center gap-1">
                <button type="button" :class="btnCls('ghost', { size: 'sm' })" @click="copySelection">
                  复制
                </button>
                <button type="button" :class="btnCls('ghost', { size: 'sm' })" @click="selectWholePage">
                  清空
                </button>
                <button type="button" :class="btnCls('primary', { size: 'sm' })" @click="askAboutSelection">
                  <PhSelectionAll :size="11" />
                  追问这段
                </button>
              </div>
            </div>
          </div>
        </div>
      </template>

      <!-- 空状态 -->
      <div v-else class="grid h-full place-items-center px-6">
        <div :class="EMPTY_CLS">
          <PhFileText :size="22" class="mb-1.5 text-ink-4" />
          <strong class="text-[13px] text-ink-2">还没有打开文献</strong>
          <span class="max-w-64 text-2xs leading-relaxed text-ink-4">
            在左侧文献库里点一篇，正文会渲染到这里；引用角标和图谱节点都能跳回对应页。
          </span>
          <button
            v-if="library.items.length"
            type="button"
            :class="btnCls('default', { size: 'sm' })"
            class="mt-2"
            @click="ui.togglePanel('library')"
          >
            <PhDownloadSimple :size="11" />
            打开文献库
          </button>
        </div>
      </div>
    </div>
  </AppPanel>
</template>
