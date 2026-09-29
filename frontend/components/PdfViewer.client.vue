<script setup lang="ts">
/**
 * PDF.js 阅读器。客户端组件（`.client` 后缀 → 只在浏览器加载）。
 *
 * 只做三件事：翻页、缩放、**跳到引用所在的页**。文本层/搜索/标注都不做 ——
 * 那些功能的成本远大于它在这里的价值，定位引用页码用不着它们。
 *
 * UI 全走 .rc-* 类（assets/css/main.css），不引组件库：这里总共 4 个按钮
 * 加 1 个数字输入，为它们引一个 UI 框架是纯负债。
 */
import type { PDFDocumentProxy } from 'pdfjs-dist'

const props = withDefaults(
  defineProps<{
    url: string
    /** 外部要求跳转到的页码（1-based）；变化即跳转 */
    targetPage?: number | null
    /** 引用片段：画在页面顶部作为提示条 */
    highlightQuote?: string
  }>(),
  { targetPage: null, highlightQuote: '' },
)

const canvas = ref<HTMLCanvasElement | null>(null)
const viewer = ref<HTMLElement | null>(null)

const doc = shallowRef<PDFDocumentProxy | null>(null)
const pageNum = ref(1)
const pageCount = ref(0)
const scale = ref(1.2)
const loading = ref(false)
const errorMessage = ref('')

let renderTask: { cancel: () => void } | null = null

async function load() {
  if (!import.meta.client || !props.url) return
  loading.value = true
  errorMessage.value = ''
  try {
    const pdfjs = await import('pdfjs-dist')
    // Vite 会把 worker 文件当作 asset 处理，这里拿到的是它的真实 URL
    pdfjs.GlobalWorkerOptions.workerSrc = new URL(
      'pdfjs-dist/build/pdf.worker.min.mjs',
      import.meta.url,
    ).toString()

    const prev = doc.value
    doc.value = await pdfjs.getDocument({ url: props.url }).promise
    pageCount.value = doc.value.numPages
    pageNum.value = 1
    await prev?.destroy()
    await render()
  } catch (err) {
    errorMessage.value = `PDF 加载失败：${(err as Error).message}`
    doc.value = null
    pageCount.value = 0
  } finally {
    loading.value = false
  }
}

async function render() {
  const d = doc.value
  const el = canvas.value
  if (!d || !el) return

  // 上一帧还在画就取消，否则快速翻页会画出错位的画面
  renderTask?.cancel()
  const page = await d.getPage(pageNum.value)
  const viewport = page.getViewport({ scale: scale.value })

  const dpr = window.devicePixelRatio || 1
  const ctx = el.getContext('2d')
  if (!ctx) return

  el.width = Math.floor(viewport.width * dpr)
  el.height = Math.floor(viewport.height * dpr)
  el.style.width = `${Math.floor(viewport.width)}px`
  el.style.height = `${Math.floor(viewport.height)}px`

  const task = page.render({
    canvasContext: ctx,
    viewport,
    transform: dpr === 1 ? undefined : [dpr, 0, 0, dpr, 0, 0],
  })
  renderTask = task
  try {
    await task.promise
  } catch {
    /* cancel() 会以异常形式结束，属正常路径 */
  }
}

/** 页码收敛到 [1, pageCount]。渲染只由 watch 触发，这里不重复调 render。 */
function goto(n: number) {
  pageNum.value = Math.min(Math.max(1, n), pageCount.value || 1)
}

function zoom(delta: number) {
  scale.value = Math.min(3, Math.max(0.5, +(scale.value + delta).toFixed(2)))
}

/** 页码框允许越界输入，失焦时收敛 —— 不收敛的话下一次翻页会从错误的位置开始 */
function normalizePageInput() {
  goto(pageNum.value || 1)
}

watch(() => props.url, () => void load())
watch([pageNum, scale], () => void render())
watch(
  () => props.targetPage,
  (p) => {
    if (p) goto(p)
  },
)

onMounted(() => void load())
onBeforeUnmount(() => {
  renderTask?.cancel()
  void doc.value?.destroy()
})
</script>

<template>
  <div class="viewer rc-panel">
    <div class="bar">
      <div class="group">
        <button
          class="rc-btn rc-btn--secondary rc-btn--sm"
          type="button"
          :disabled="pageNum <= 1"
          @click="goto(pageNum - 1)"
        >
          上一页
        </button>
        <button
          class="rc-btn rc-btn--secondary rc-btn--sm"
          type="button"
          :disabled="pageNum >= pageCount"
          @click="goto(pageNum + 1)"
        >
          下一页
        </button>
      </div>

      <span class="rc-mono rc-muted">
        <input
          v-model.number="pageNum"
          class="page-input"
          type="number"
          min="1"
          :max="pageCount || 1"
          aria-label="页码"
          @change="normalizePageInput"
        />
        / {{ pageCount || '—' }}
      </span>

      <div class="group">
        <button class="rc-btn rc-btn--secondary rc-btn--icon rc-btn--sm" type="button" aria-label="缩小" @click="zoom(-0.2)">
          −
        </button>
        <span class="rc-mono rc-muted">{{ (scale * 100).toFixed(0) }}%</span>
        <button class="rc-btn rc-btn--secondary rc-btn--icon rc-btn--sm" type="button" aria-label="放大" @click="zoom(0.2)">
          +
        </button>
      </div>

      <span class="rc-spacer" />
      <a :href="url" target="_blank" rel="noopener" class="rc-caption">在新标签打开</a>
    </div>

    <p v-if="highlightQuote" class="quote-bar">
      <b>引用片段：</b>{{ highlightQuote }}
    </p>

    <p v-if="errorMessage" class="rc-alert rc-alert--bad notice">{{ errorMessage }}</p>

    <div ref="viewer" class="canvas-wrap rc-scroll">
      <canvas ref="canvas" />
      <div v-if="loading" class="busy">
        <span class="rc-spin" />
        <span class="rc-caption">正在渲染…</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.viewer {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--rc-hairline);
  background: var(--rc-surface-1);
}

.group {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.page-input {
  width: 44px;
  height: 26px;
  padding: 0 4px;
  background: var(--rc-surface-1);
  color: var(--rc-ink);
  border: 1px solid var(--rc-hairline);
  border-radius: var(--rc-radius-sm);
  text-align: center;
  font-family: var(--rc-mono);
  font-size: 12px;
}
.page-input::-webkit-outer-spin-button,
.page-input::-webkit-inner-spin-button {
  appearance: none;
  margin: 0;
}
.page-input:focus {
  outline: none;
  border-color: var(--rc-primary-focus);
  box-shadow: var(--rc-ring);
}

.quote-bar {
  margin: 0;
  padding: 7px 11px;
  background: var(--rc-warn-soft);
  border-bottom: 1px solid var(--rc-warn);
  font-size: 12.5px;
  color: var(--rc-warn);
  max-height: 66px;
  overflow: hidden;
}

.notice {
  margin: 8px 10px;
}

/* 画布底衬：PDF 纸面是白的，这里的底色只负责把它衬托出来，不抢戏 */
.canvas-wrap {
  position: relative;
  flex: 1;
  display: flex;
  justify-content: center;
  padding: 14px;
  background: var(--rc-canvas);
}
.canvas-wrap canvas {
  height: fit-content;
  border-radius: 2px;
  box-shadow: var(--rc-shadow-pop);
}

.busy {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: color-mix(in srgb, var(--rc-canvas) 55%, transparent);
}
</style>
