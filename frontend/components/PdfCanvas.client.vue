<script setup lang="ts">
/**
 * PDF 页面渲染器（客户端组件）。
 *
 * 三件事：**画出来**（canvas + 文本层）、**能划词**（把浏览器选区换算成归一化矩形）、
 * **能定位**（按引用片段找到并高亮对应的文本 run）。
 *
 * 关键设计：坐标一律存**归一化值**（相对页面的 0~1 比例），不存像素。
 * 用户随时会缩放、切适配宽度，存像素的高亮框在下一次渲染后就会飘走。
 *
 * 文本层用的是 pdfjs 官方的 `TextLayer`（v4 起是类，替换了旧的 renderTextLayer）。
 * 它把每个文本 run 定位成透明 absolute span —— 于是浏览器原生划词、双击选词、
 * 「复制」全部免费获得，不需要自己实现选区逻辑。
 */
import type { PDFDocumentProxy, RenderTask } from 'pdfjs-dist'
import type { ReaderTarget, NormRect } from '~/types/workbench'

const props = withDefaults(
  defineProps<{
    url: string
    paperId: string
    /** 当前页（1-based），由父组件持有 */
    page: number
    /** 缩放倍率。fitWidth 为真时此值被忽略 */
    scale?: number
    /** 适配宽度：按滚动容器宽度反算缩放 */
    fitWidth?: boolean
    /** 跳转指令。只认 nonce，这样连点同一处引用也能再次定位 */
    target?: ReaderTarget | null
  }>(),
  { scale: 1.2, fitWidth: false, target: null },
)

const emit = defineEmits<{
  'update:page': [value: number]
  'update:scale': [value: number]
  'update:pageCount': [value: number]
  /** 划词产生的锚点 */
  select: [selection: { paperId: string; page: number; text: string; rects: NormRect[] }]
}>()

const scroller = ref<HTMLElement | null>(null)
const pageEl = ref<HTMLElement | null>(null)
const canvasEl = ref<HTMLCanvasElement | null>(null)
const textLayerEl = ref<HTMLElement | null>(null)

const doc = shallowRef<PDFDocumentProxy | null>(null)
const pageCount = ref(0)
const loading = ref(false)
const errorMessage = ref('')

/** 当前页尺寸（CSS px） */
const cssSize = reactive({ w: 0, h: 0 })
/** 高亮框（归一化）。手动划词与引用定位共用这套渲染。 */
const highlightRects = ref<NormRect[]>([])

let renderTask: RenderTask | null = null
// TextLayer 类型来自 pdfjs 内部命名空间，显式声明成结构类型避免深度 import
let textLayer: { cancel: () => void } | null = null
let renderToken = 0

// ---------------------------------------------------------------- 加载

async function load() {
  if (!import.meta.client || !props.url) return
  loading.value = true
  errorMessage.value = ''
  highlightRects.value = []
  try {
    const pdfjs = await import('pdfjs-dist')
    // Vite 会把 worker 文件当 asset 处理，这里拿到的是它的真实 URL
    pdfjs.GlobalWorkerOptions.workerSrc = new URL(
      'pdfjs-dist/build/pdf.worker.min.mjs',
      import.meta.url,
    ).toString()

    const prev = doc.value
    doc.value = await pdfjs.getDocument({ url: props.url }).promise
    pageCount.value = doc.value.numPages
    emit('update:pageCount', pageCount.value)
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

// ---------------------------------------------------------------- 渲染

/**
 * 适配宽度时的缩放：滚动容器可用宽度 ÷ 页面在 scale=1 时的宽度。
 * 减掉两侧的 padding 与 1px 边框，否则会出现横向滚动条这个"差一点"的经典问题。
 */
async function computeFitScale(): Promise<number> {
  const d = doc.value
  const box = scroller.value
  if (!d || !box) return props.scale
  const p = await d.getPage(props.page)
  const base = p.getViewport({ scale: 1 }).width
  const avail = box.clientWidth - 32
  return Math.max(0.25, Math.min(4, avail / base))
}

async function render() {
  const d = doc.value
  const canvas = canvasEl.value
  const layer = textLayerEl.value
  if (!d || !canvas || !layer) return

  const token = ++renderToken
  // 上一帧还在画就取消，否则快速翻页会画出错位的画面
  renderTask?.cancel()
  textLayer?.cancel()

  const effectiveScale = props.fitWidth ? await computeFitScale() : props.scale
  if (token !== renderToken) return // 等待期间又翻页了
  if (props.fitWidth && Math.abs(effectiveScale - props.scale) > 0.001) emit('update:scale', effectiveScale)

  const pdfPage = await d.getPage(props.page)
  if (token !== renderToken) return
  const viewport = pdfPage.getViewport({ scale: effectiveScale })

  cssSize.w = Math.floor(viewport.width)
  cssSize.h = Math.floor(viewport.height)

  const dpr = window.devicePixelRatio || 1
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  canvas.width = Math.floor(viewport.width * dpr)
  canvas.height = Math.floor(viewport.height * dpr)
  canvas.style.width = `${cssSize.w}px`
  canvas.style.height = `${cssSize.h}px`

  const task = pdfPage.render({
    canvasContext: ctx,
    viewport,
    // 高分屏下按 dpr 放大再让 CSS 缩回去，字才不会发虚
    transform: dpr === 1 ? undefined : [dpr, 0, 0, dpr, 0, 0],
  })
  renderTask = task
  try {
    await task.promise
  } catch {
    return // cancel() 会以异常形式结束，属正常路径
  }
  if (token !== renderToken) return

  // 文本层：先清空，否则翻页会叠上一层看不见的旧 span，划词选中空气
  layer.replaceChildren()
  const { TextLayer } = await import('pdfjs-dist')
  const tl = new TextLayer({
    textContentSource: pdfPage.streamTextContent(),
    container: layer,
    viewport,
  })
  textLayer = tl
  await tl.render()
  if (token !== renderToken) return

  // 换页/换缩放后把上一次的引用定位重放一遍，否则点完引用一缩放高亮就没了
  if (props.target?.quote && props.target.page === props.page) revealQuote(props.target.quote)
}

// ---------------------------------------------------------------- 划词

/**
 * 浏览器选区 → 归一化矩形。
 *
 * 用 `getClientRects()` 而不是 `getBoundingClientRect()`：跨行选择时前者给的是
 * 每行一个矩形（多行高亮才对），后者只给一个大包围盒（会连行尾空白一起框住）。
 */
function onMouseUp() {
  const layer = textLayerEl.value
  const box = pageEl.value
  if (!layer || !box) return

  const sel = window.getSelection()
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return
  const range = sel.getRangeAt(0)
  if (!layer.contains(range.commonAncestorContainer)) return

  const text = sel.toString().trim()
  if (!text) return

  const pageBox = box.getBoundingClientRect()
  const rects: NormRect[] = []
  for (const r of Array.from(range.getClientRects())) {
    if (r.width < 1 || r.height < 1) continue
    const rect = {
      x: (r.left - pageBox.left) / pageBox.width,
      y: (r.top - pageBox.top) / pageBox.height,
      w: r.width / pageBox.width,
      h: r.height / pageBox.height,
    }
    // 丢掉跑到页面外的碎片（跨页拖选时会出现）
    if (rect.y < -0.02 || rect.y > 1.02 || rect.x < -0.02 || rect.x > 1.02) continue
    rects.push(rect)
  }
  if (!rects.length) return

  emit('select', { paperId: props.paperId, page: props.page, text, rects })
}

// ---------------------------------------------------------------- 引用定位

/**
 * 在文本层里找到引用片段，并把它对应的矩形高亮出来。
 *
 * 难点：`quote` 往往横跨多个文本 run（pdfjs 是按行/按字形段切 run 的），
 * 所以不能拿单个 span 的 textContent 去 match。做法是把按顺序拼接后的
 * **去空白全文**当成一个大串，在上面找 needle，再反查是哪些 span 落在区间里。
 * 去空白是必须的 —— PDF 里中文之间常有空格、英文断词处有连字符，
 * 不归一化的话"看起来一样"的两段文字匹配不上。
 */
function revealQuote(quote: string) {
  const layer = textLayerEl.value
  if (!layer) return
  const spans = Array.from(layer.querySelectorAll<HTMLElement>('span'))
  const norm = (s: string) => s.replace(/\s+/g, '')
  const needle = norm(quote).slice(0, 30)
  if (needle.length < 4) return

  const parts: { el: HTMLElement; start: number; end: number }[] = []
  let acc = ''
  for (const el of spans) {
    const t = norm(el.textContent || '')
    if (!t) continue
    parts.push({ el, start: acc.length, end: acc.length + t.length })
    acc += t
  }
  const at = acc.indexOf(needle)
  if (at < 0) return
  const to = at + needle.length

  const hit = parts.filter((p) => p.end > at && p.start < to)
  if (!hit.length) return

  const box = pageEl.value?.getBoundingClientRect()
  if (!box) return
  const rects: NormRect[] = []
  for (const p of hit) {
    const r = p.el.getBoundingClientRect()
    if (r.width < 1 || r.height < 1) continue
    rects.push({
      x: (r.left - box.left) / box.width,
      y: (r.top - box.top) / box.height,
      w: r.width / box.width,
      h: r.height / box.height,
    })
  }
  if (rects.length) {
    highlightRects.value = rects
    hit[0].el.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }
}

function rectStyle(r: NormRect) {
  return {
    left: `${r.x * 100}%`,
    top: `${r.y * 100}%`,
    width: `${r.w * 100}%`,
    height: `${r.h * 100}%`,
  }
}

// ---------------------------------------------------------------- 副作用

/** 跨页跳转时，quote 要等新页渲染完才找得到，先存下来。 */
const pendingQuote = ref('')

/** 外部跳转：page 变了就翻页，quote 有就再定位一次。 */
watch(
  () => props.target?.nonce,
  () => {
    const t = props.target
    if (!t || t.paperId !== props.paperId) return
    if (t.page !== props.page) {
      // 翻页由 page 变化触发 render，render 结束时自己会 revealQuote
      pendingQuote.value = t.quote ?? ''
      emit('update:page', t.page)
    } else if (t.quote) {
      revealQuote(t.quote)
    }
  },
)

watch(
  () => props.page,
  () => {
    highlightRects.value = []
    void render().then(() => {
      if (pendingQuote.value) {
        revealQuote(pendingQuote.value)
        pendingQuote.value = ''
      }
    })
  },
)

watch(
  () => [props.scale, props.fitWidth],
  () => void render(),
)

watch(
  () => props.url,
  () => void load(),
)

// 面板拖宽了，适配宽度得跟着重算 —— 不加这个，拖宽面板 PDF 不会跟着变大
let observer: ResizeObserver | null = null

onMounted(() => {
  void load()
  if (import.meta.client && typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(() => {
      if (props.fitWidth) void render()
    })
    if (scroller.value) observer.observe(scroller.value)
  }
})

onBeforeUnmount(() => {
  renderToken++ // 让在途的异步渲染失效
  observer?.disconnect()
  observer = null
  renderTask?.cancel()
  textLayer?.cancel()
  void doc.value?.destroy()
})

// 父组件（工具条的"适配宽度"按钮）需要主动触发一次重算
defineExpose({ render, revealQuote })
</script>

<template>
  <div ref="scroller" class="h-full overflow-auto scroll-slim bg-sunken">
    <div class="flex min-h-full justify-center p-4">
      <div
        ref="pageEl"
        class="relative shrink-0 bg-white shadow-md"
        :style="cssSize.w ? { width: `${cssSize.w}px`, height: `${cssSize.h}px` } : undefined"
        @mouseup="onMouseUp"
      >
        <canvas ref="canvasEl" class="block" />

        <!-- 文本层：透明、可选中。样式在 assets/css/main.css 的 .pdf-text-layer -->
        <div ref="textLayerEl" class="pdf-text-layer" />

        <!-- 高亮层：不吃指针事件，否则会挡住划词 -->
        <div class="pointer-events-none absolute inset-0">
          <span
            v-for="(r, i) in highlightRects"
            :key="i"
            class="absolute rounded-[1px] bg-brand/22 ring-1 ring-brand/35 ring-inset"
            :style="rectStyle(r)"
          />
        </div>

        <div
          v-if="loading"
          class="absolute inset-0 grid place-items-center bg-white/60 backdrop-blur-[1px]"
        >
          <AppSpinner :size="18" class="text-ink-3" />
        </div>
      </div>
    </div>

    <p v-if="errorMessage" class="mx-4 mb-4 rounded-md bg-bad-soft px-3 py-2 text-2xs text-bad">
      {{ errorMessage }}
    </p>
    <p v-else-if="pageCount && !cssSize.w && !loading" class="mx-4 text-2xs text-ink-4">
      正在准备页面…
    </p>
  </div>
</template>
