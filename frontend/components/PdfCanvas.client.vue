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
import type { ReaderTarget, NormRect, HighlightRef } from '~/types/workbench'

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
    /** 段落级高亮：`[{id, page, bbox}]`，只画当前页的。双语对照用 */
    highlights?: HighlightRef[]
    /** `highlights` 里哪一条是"当前段"（实心强调），其余画成淡底 */
    activeId?: string | null
    /** 左侧缩略图轨 */
    thumbs?: boolean
  }>(),
  { scale: 1.2, fitWidth: false, target: null, highlights: () => [], activeId: null, thumbs: false },
)

const emit = defineEmits<{
  'update:page': [value: number]
  'update:scale': [value: number]
  'update:pageCount': [value: number]
  /** 划词产生的锚点 */
  select: [selection: { paperId: string; page: number; text: string; rects: NormRect[] }]
  /**
   * 滚动位置。`y` 是**视口中心对应的页内归一化纵坐标**（0~1）。
   *
   * 给的是 `y` 而不是 `scrollTop`：对侧要的是"现在在看哪一段"，像素值离开这个
   * 容器的尺寸就没有意义了，缩放一下全部作废。
   */
  scroll: [position: { page: number; y: number; top: number }]
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
  // 换了文档，缩略图的"画过了"标记必须清掉，否则新文档会沿用旧文档的缩略图
  railEl.value?.querySelectorAll('canvas').forEach((c) => delete (c as HTMLElement).dataset.done)
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

  // 换页/换缩放后把上一次的定位重放一遍，否则点完引用一缩放高亮就没了
  if (props.target && props.target.page === props.page) replayTarget(props.target)

  // 页尺寸变了，滚动位置也得重新广播一次（对侧按 `y` 比例对齐）
  emitScroll()
  void drawThumbs()
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

/** 按坐标画框并把它滚到视野中间。 */
function highlightByRects(rects: NormRect[]) {
  if (!rects.length) return
  highlightRects.value = rects
  const scrollerEl = scroller.value
  const pageBox = pageEl.value?.getBoundingClientRect()
  if (!scrollerEl || !pageBox) return
  const first = rects[0]
  const top = pageBox.top - scrollerEl.getBoundingClientRect().top + (first.y + first.h / 2) * pageBox.height
  scrollerEl.scrollTo({ top: Math.max(0, top - scrollerEl.clientHeight / 2), behavior: 'smooth' })
}

/** 重放一次定位指令：有 bbox 坐标走坐标，否则退回按 quote 文字匹配。 */
function replayTarget(t: ReaderTarget) {
  if (t.rects?.length) highlightByRects(t.rects)
  else if (t.quote) revealQuote(t.quote)
}

function rectStyle(r: NormRect) {
  return {
    left: `${r.x * 100}%`,
    top: `${r.y * 100}%`,
    width: `${r.w * 100}%`,
    height: `${r.h * 100}%`,
  }
}

// ---------------------------------------------------------------- 段落高亮（双语对照）

/** 当前页上要画的段落框。跨页的段落在翻页前先不画 —— 画出来只会是一片错位的紫块。 */
const segBoxes = computed(() =>
  (props.highlights ?? [])
    .filter((h) => h.page === props.page)
    .map((h) => ({ id: h.id, active: h.id === props.activeId, rects: rectsFromBbox(h.bbox) }))
    .filter((b) => b.rects.length > 0),
)

// ---------------------------------------------------------------- 滚动位置

let rafId = 0

/** 视口中心落在页内哪个归一化高度 —— 对侧拿它去挑"最接近的那一段"。 */
function emitScroll() {
  const el = scroller.value
  if (!el) return
  const box = pageEl.value?.getBoundingClientRect()
  const center = el.getBoundingClientRect().top + el.clientHeight / 2
  const y = box && box.height ? Math.min(1, Math.max(0, (center - box.top) / box.height)) : 0
  emit('scroll', { page: props.page, y, top: el.scrollTop })
}

/** 一帧一次。scroll 事件的频率远高于渲染帧，不节流的话每像素都要算一次布局。 */
function onScroll() {
  if (rafId) return
  rafId = requestAnimationFrame(() => {
    rafId = 0
    emitScroll()
  })
}

// ---------------------------------------------------------------- 缩略图

const railEl = ref<HTMLElement | null>(null)

/**
 * 画一张缩略图。`data-done` 做一次性标记 —— 重绘一次白花几十毫秒，
 * 而缩略图只在换文档时才会变。
 */
async function drawThumb(canvas: HTMLCanvasElement) {
  const d = doc.value
  if (!d || canvas.dataset.done) return
  canvas.dataset.done = '1'
  const num = Number(canvas.dataset.page)
  if (!Number.isInteger(num) || num < 1) return
  try {
    const p = await d.getPage(num)
    const base = p.getViewport({ scale: 1 })
    // 宽度按轨道实际宽度反算，缩略图才不会糊（写死 80px 在宽轨道上会糊成一团）
    const width = Math.max(60, railEl.value?.clientWidth ? railEl.value.clientWidth - 16 : 80)
    const viewport = p.getViewport({ scale: width / base.width })
    const dpr = window.devicePixelRatio || 1
    canvas.width = Math.floor(viewport.width * dpr)
    canvas.height = Math.floor(viewport.height * dpr)
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    await p
      .render({
        canvasContext: ctx,
        viewport,
        transform: dpr === 1 ? undefined : [dpr, 0, 0, dpr, 0, 0],
      })
      .promise.catch(() => undefined)
  } catch {
    // 单张缩略图失败不该影响阅读，静默留白即可
    delete canvas.dataset.done
  }
}

/**
 * 串行画完所有缩略图。
 *
 * ponytail: 没有按需渲染 —— 一次 20 页的论文量级是百毫秒级，比"先接
 * IntersectionObserver 再调半天可见性"便宜得多。页数上百再换成按需。
 */
async function drawThumbs() {
  if (!props.thumbs || !doc.value) return
  const canvases = Array.from(railEl.value?.querySelectorAll<HTMLCanvasElement>('canvas') ?? [])
  for (const c of canvases) await drawThumb(c)
}

// ---------------------------------------------------------------- 副作用

/** 跨页跳转时，定位指令要等新页渲染完才用得上，先存下来。 */
const pendingTarget = ref<ReaderTarget | null>(null)

/** 外部跳转：page 变了就翻页，同页则直接定位一次。 */
watch(
  () => props.target?.nonce,
  () => {
    const t = props.target
    if (!t || t.paperId !== props.paperId) return
    if (t.page !== props.page) {
      // 翻页由 page 变化触发 render，render 结束时自己会重放定位
      pendingTarget.value = t
      emit('update:page', t.page)
    } else {
      replayTarget(t)
    }
  },
)

watch(
  () => props.page,
  () => {
    highlightRects.value = []
    void render().then(() => {
      if (pendingTarget.value) {
        replayTarget(pendingTarget.value)
        pendingTarget.value = null
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

// 缩略图轨是后开的：canvas 得等 DOM 出来才画得上
watch(
  () => props.thumbs,
  async (on) => {
    if (!on) return
    await nextTick()
    void drawThumbs()
  },
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
  <div class="flex h-full min-h-0">
    <!-- 缩略图轨：点哪张跳哪页 -->
    <div
      v-if="thumbs"
      ref="railEl"
      class="w-[104px] shrink-0 space-y-1.5 overflow-y-auto scroll-slim border-r border-hairline bg-surface p-2"
    >
      <button
        v-for="n in pageCount"
        :key="n"
        type="button"
        class="relative block w-full overflow-hidden rounded-sm ring-1 transition-shadow"
        :class="n === page ? 'ring-2 ring-brand' : 'ring-hairline hover:ring-hairline-2'"
        :title="`第 ${n} 页`"
        @click="emit('update:page', n)"
      >
        <canvas :data-page="n" class="block w-full bg-white" />
        <span class="absolute bottom-0 right-0 rounded-tl-sm bg-ink/55 px-1 text-[9px] text-white">{{ n }}</span>
      </button>
      <p v-if="!pageCount" class="py-4 text-center text-[10px] text-ink-4">—</p>
    </div>

    <div
      ref="scroller"
      data-pdf-scroller
      class="h-full min-w-0 flex-1 overflow-auto scroll-slim bg-sunken"
      @scroll.passive="onScroll"
    >
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

          <!-- 段落高亮层：先画淡底，当前段再叠一层实心的（不吃指针事件，否则挡住划词） -->
          <div class="pointer-events-none absolute inset-0">
            <template v-for="b in segBoxes" :key="b.id">
              <span
                v-for="(r, i) in b.rects"
                :key="i"
                :data-hl="b.active ? b.id : undefined"
                class="absolute rounded-[2px]"
                :class="b.active ? 'bg-brand/25 ring-1 ring-brand/55 ring-inset' : 'bg-brand/8 ring-1 ring-brand/15 ring-inset'"
                :style="rectStyle(r)"
              />
            </template>
          </div>

          <!-- 高亮层：划词 / 引用定位 -->
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
  </div>
</template>
