<script setup lang="ts">
/**
 * 中英双向联动：左 PDF / 右译文，按**段落**对齐。
 *
 * 为什么锚在段落而不是滚动比例：译文和原文的长度差得多（中英字数比约 1:1.7），
 * 按 `scrollTop` 百分比换算，滚到中段就开始漂，越往后越离谱。所以段落 ID 由后端
 * 切分时定死并原样回传（见 `app/api/v1/translate.py` 的 `blocks` 分支），
 * 前端只做一件事：把同 ID 的两段摆到同一相对位置。
 *
 * 三个东西配合，缺一个就同步不了：
 * - 右栏用 IntersectionObserver 开一条**探针带**（视口上方 8%~16%）。带的作用只是
 *   "把候选压缩到 2 段"，真正判定当前段的是带正中那条**阅读线**跨过了谁（见 `pickAtProbe`）——
 *   比在 scroll 里遍历 200 个 `offsetTop` 便宜，也不怕段被撑开；
 * - 左栏由 `PdfCanvas` 抛出**页内归一化纵坐标** `y`，拿它去挑同页里 bbox 中心最接近的段；
 * - `lockUntil` 时间窗（不是布尔量）挡住回音室：程序化滚动会触发对侧的 scroll/IO 回调，
 *   两边一旦互相接话就会来回抽搐。
 */

import type { TranslatedParagraph } from '~/types/api'
import type { HighlightRef, ReaderTarget } from '~/types/workbench'
import type { BandBox } from '~/utils/ui'

const props = defineProps<{
  /** 后端 `POST /translate` 的 `paragraphs`，带 page / bbox */
  segments: TranslatedParagraph[]
  paperId: string
  pdfUrl: string
}>()

/** 同步窗口（毫秒）。它同时就是"防抖"：窗口内的事件一律忽略。 */
const SYNC_MS = 100
/** 平滑滚动跑完约 300ms，期间对侧的回调必须闭嘴，否则会打断动画并来回拉锯。 */
const SMOOTH_MS = 450
/**
 * 一次"跳转"（右栏点段 / 右栏滚到新段 → 把 PDF 翻过去）之后，PDF 侧的滚动事件要闭嘴多久。
 *
 * 必须比 `SMOOTH_MS` 长得多，因为它还要盖住"翻页 → 渲染 → 再滚到目标框"这整条异步链。
 * 不盖住的后果实测过：跳转后 PDF 自己 emit 一个 y，拿它反推"在读哪一段"，
 * 而在页尾附近目标框**居中不了**（滚动被钳位），y 就漂到了下一段 —— 刚对齐好的高亮
 * 被自己顶成下一段，观感是"点了第 4 段却高亮第 5 段"。
 */
const JUMP_MS = 900

/**
 * 探针带 = 视口高度的 8%~16%，**带的正中（12%）那条线**是"阅读线"。
 *
 * 注意带里通常不止一段：带只有 8% 高（实测约 40px），而一段常有 100px 以上，
 * 上一段的尾巴必然也压在带里。所以"当前段"必须由 `pickAtProbe()` 按谁跨过阅读线来定，
 * 不能取带内列表顺序上的第一个。
 *
 * `scrollRightTo` 要把目标段的**顶边**摆到阅读线上，与这三个常量一起改；
 * 摆到带外的话同步完成后带里空空如也，下一次 IO 回调会挑不出段，
 * 表现为"滚一下右栏就不动了"。
 */
const BAND_TOP = 0.08
const BAND_BOT = 0.16
/** 阅读线：带的正中。 */
const BAND_MID = (BAND_TOP + BAND_BOT) / 2
const BAND_MARGIN = `-${BAND_TOP * 100}% 0px -${(1 - BAND_BOT) * 100}% 0px`

const activeId = ref<string | null>(null)
const page = ref(1)
const mobilePane = ref<'pdf' | 'text'>('text')
let lockUntil = 0
/** 右栏跳转后 PDF 侧的静默截止时刻（见 `JUMP_MS`）。 */
let jumpUntil = 0

const paneEl = ref<HTMLElement | null>(null)

/** 有 bbox 的段才画框 —— 没有坐标的那些（公式块、图注）在 PDF 上无处可画。 */
const highlights = computed<HighlightRef[]>(() =>
  props.segments
    .filter((s) => s.page && s.bbox)
    .map((s) => ({ id: s.id, page: s.page as number, bbox: s.bbox })),
)

const active = computed(() => props.segments.find((s) => s.id === activeId.value) ?? null)

// ---------------------------------------------------------------- 左栏 → 右栏（PDF 滚动驱动）

function onPdfScroll(pos: { page: number; y: number; top: number }) {
  // 位置无条件记下（含被锁吞掉的那些）—— 补跑时要用的是**最后一次**的位置，
  // 拿被吞那一刻的旧位置补跑等于把对侧停在半路
  lastPdf = pos
  syncFromPdf()
}

/**
 * 锁到期后补跑一次。
 *
 * 没有这个会有一个很难查的 bug：程序化平滑滚动期间对侧的事件全被锁吞掉，
 * 而浏览器只在**变化**时才回调 —— 用户停手之后什么都不会再发生，
 * 于是"停在某个位置，另一侧就是不跟过去"。补跑一次才闭环。
 */
function defer(fn: () => void) {
  retryFn = fn // 后到的那次覆盖前一次：等锁解开时要跑的是最新的意图
  if (retry) return
  retry = setTimeout(() => {
    retry = null
    const run = retryFn
    retryFn = null
    run?.()
  }, SYNC_MS + 20)
}

let lastPdf = { page: 1, y: 0, top: 0 }
let retry: ReturnType<typeof setTimeout> | null = null
let retryFn: (() => void) | null = null

/** PDF → 右栏。 */
function syncFromPdf() {
  // 刚由右栏跳过来：这期间 PDF 的滚动位置是**我们自己**设的，不是用户在滚。
  // 直接吞掉，而且**不 defer** —— 跳转意图就是权威，反推只会把刚对齐好的段落顶掉。
  if (Date.now() < jumpUntil) return
  const seg = nearestByY(props.segments, lastPdf.page, lastPdf.y)
  if (!seg || seg.id === activeId.value) return
  if (Date.now() < lockUntil) return defer(syncFromPdf)
  lockUntil = Date.now() + SMOOTH_MS
  activeId.value = seg.id
  scrollRightTo(seg.id)
}

/** 把右栏对应段滚到探针带正中 —— 同步完它仍在带里，不会因为落点在带外而失联。 */
function scrollRightTo(id: string) {
  const pane = paneEl.value
  const el = pane?.querySelector<HTMLElement>(`[data-seg="${id}"]`)
  if (!pane || !el) return
  // 用 rect 差值算，不用 `el.offsetTop`：offsetTop 是相对「最近的定位祖先」的，
  // 面板自己没 position 时它就跨了一层，落点会整体偏出整整一段的距离
  const delta = el.getBoundingClientRect().top - pane.getBoundingClientRect().top
  const want = Math.max(0, pane.scrollTop + delta - pane.clientHeight * BAND_MID)
  if (Math.abs(want - pane.scrollTop) < 2) return
  pane.scrollTo({ top: want, behavior: 'smooth' })
}

// ---------------------------------------------------------------- 右栏 → 左栏（译文滚动驱动）

/** 落在探针带里的段。IO 回调不保证顺序，所以只记集合，顺序回到 `segments` 上找。 */
const band = new Set<string>()
let io: IntersectionObserver | null = null

function setupObserver() {
  io?.disconnect()
  band.clear()
  const root = paneEl.value
  if (!root) return
  io = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        const id = (e.target as HTMLElement).dataset.seg
        if (!id) continue
        if (e.isIntersecting) band.add(id)
        else band.delete(id)
      }
      syncFromRight()
    },
    { root, rootMargin: BAND_MARGIN },
  )
  root.querySelectorAll<HTMLElement>('[data-seg]').forEach((el) => io!.observe(el))
}

/** 右栏 → PDF。 */
function syncFromRight() {
  const seg = atBottom() ? (props.segments.at(-1) ?? null) : pickAtProbe()
  if (!seg || seg.id === activeId.value) return
  if (Date.now() < lockUntil) return defer(syncFromRight)
  lockUntil = Date.now() + SMOOTH_MS
  focusPdf(seg)
}

/**
 * 右栏滚到底了没有 —— 且**确实有可滚的余量**。
 *
 * 到底之后阅读线够不着最后几段：滚动被钳位，线只能停在倒数第三段的区间里，
 * 于是"滚到底"高亮的是中间那一段，而人眼盯着的是屏幕最下面那段 —— 看着就是不准确。
 * 到底就用最后一段；中间过程照旧交给 `pickAtProbe`。
 *
 * `余量 > 0` 这个条件不能省：窄屏下切到 PDF 栏时译文栏是 `display:none`，
 * 此时 `clientHeight` 与 `scrollHeight` 都是 0，写成 `0 >= 0 - 2` 就成了"到底了"，
 * 于是每切一次栏，PDF 就被顶到**最后一段**（实测：页面 1 → 2、高亮变 `p2c4`）。
 */
function atBottom(): boolean {
  const pane = paneEl.value
  if (!pane) return false
  const room = pane.scrollHeight - pane.clientHeight
  return room > 0 && pane.scrollTop >= room - 2
}

/**
 * 探针线（带正中）落在哪一段里。
 *
 * 判定规则在 `pickAtLine` 里（纯函数，`npm run check:sync` 盯着它）：带内的候选通常
 * 不止一段，必须按"谁跨过阅读线"选，不能取列表顺序上的第一个。
 */
function pickAtProbe(): TranslatedParagraph | null {
  const pane = paneEl.value
  // 面板被藏起来（窄屏切到 PDF 栏）时高度是 0：阅读线会落到 0，而隐藏元素的高度也是 0，
  // 于是"谁跨过这条线"退化成"每条都跨过"，挑出来的段与用户看的毫无关系
  if (!pane || !band.size || !pane.clientHeight) return null
  const base = pane.getBoundingClientRect().top
  const boxes: (BandBox & { seg: TranslatedParagraph })[] = []
  for (const id of band) {
    const el = pane.querySelector<HTMLElement>(`[data-seg="${id}"]`)
    const seg = props.segments.find((s) => s.id === id)
    if (!el || !seg) continue
    const r = el.getBoundingClientRect()
    boxes.push({ id, top: r.top - base, bottom: r.bottom - base, seg })
  }
  return pickAtLine(boxes, pane.clientHeight * BAND_MID)?.seg ?? null
}

function focusPdf(seg: TranslatedParagraph) {
  activeId.value = seg.id
  jumpUntil = Date.now() + JUMP_MS
  const rects = rectsFromBbox(seg.bbox)
  if (seg.page) page.value = seg.page
  if (!rects.length) return // 没坐标只能翻页，画不出框
  // 复用阅读器既有的跳转指令（带 nonce，连点同一段也能再次定位）
  target.value = { paperId: props.paperId, page: seg.page ?? 1, rects, nonce: ++nonce }
}

const target = ref<ReaderTarget | null>(null)
let nonce = 0

/** 手动点段落：只对右栏负责，PDF 那边立刻定位。 */
function pick(id: string) {
  const seg = props.segments.find((s) => s.id === id)
  if (!seg) return
  lockUntil = Date.now() + SMOOTH_MS
  focusPdf(seg)
}

// ---------------------------------------------------------------- 副作用

watch(
  () => [props.segments, paneEl.value] as const,
  async () => {
    lockUntil = 0
    await nextTick()
    setupObserver()
  },
)

watch(
  () => props.segments,
  () => {
    activeId.value = null
    target.value = null
    jumpUntil = 0
  },
)

onBeforeUnmount(() => {
  io?.disconnect()
  if (retry) clearTimeout(retry)
})
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <!-- 小屏一次只看一栏：两栏各半屏谁都读不了 -->
    <div class="flex shrink-0 items-center gap-1 border-b border-hairline bg-surface px-2 py-1.5 md:hidden">
      <Segmented
        v-model="mobilePane"
        size="sm"
        :options="[
          { value: 'pdf', label: '原文 PDF' },
          { value: 'text', label: '译文' },
        ]"
        aria-label="显示哪一栏"
      />
      <span class="min-w-0 flex-1 truncate text-2xs text-ink-4">{{ active?.id ?? '' }}</span>
    </div>

    <div class="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-2">
      <!-- 原文 -->
      <section
        class="min-h-0 flex-col border-hairline md:flex md:border-r"
        :class="mobilePane === 'pdf' ? 'flex' : 'hidden'"
      >
        <ClientOnly>
          <PdfViewer
            v-model:page="page"
            :url="pdfUrl"
            :paper-id="paperId"
            :highlights="highlights"
            :active-id="activeId"
            :target="target"
            @scroll="onPdfScroll"
          />
          <template #fallback>
            <div class="grid h-full place-items-center bg-sunken">
              <AppSpinner :size="18" class="text-ink-3" />
            </div>
          </template>
        </ClientOnly>
      </section>

      <!-- 译文 -->
      <section
        class="min-h-0 flex-col md:flex"
        :class="mobilePane === 'text' ? 'flex' : 'hidden'"
      >
        <div class="flex h-8 shrink-0 items-center gap-1.5 border-b border-hairline bg-surface px-3">
          <span class="text-2xs font-semibold text-ink-2">译文</span>
          <span :class="pillCls('default')">{{ segments.length }} 段</span>
          <span class="min-w-0 flex-1 truncate text-2xs text-ink-4">
            {{ active ? `${active.id} · 第 ${active.page ?? '—'} 页` : '滚动任一侧，另一侧会跟过去' }}
          </span>
        </div>

        <div ref="paneEl" class="min-h-0 flex-1 overflow-y-auto scroll-slim px-3 py-3">
          <article
            v-for="s in segments"
            :key="s.id"
            :data-seg="s.id"
            :data-active="s.id === activeId ? '1' : undefined"
            class="mb-2 cursor-pointer rounded-md border px-2.5 py-2 transition-colors"
            :class="
              s.id === activeId
                ? 'border-brand/45 bg-brand-soft/50'
                : 'border-hairline bg-surface hover:border-hairline-2 hover:bg-hover'
            "
            @click="pick(s.id)"
          >
            <div class="mb-1 flex items-center gap-1.5">
              <span class="font-mono text-[10.5px] text-ink-4">{{ s.id }}</span>
              <span v-if="s.page" :class="pillCls('default')" class="tabular-nums">p{{ s.page }}</span>
              <span v-if="!s.target.trim()" :class="pillCls('warn')">空译文</span>
            </div>
            <p class="text-[12.5px] leading-relaxed whitespace-pre-wrap text-ink">{{ s.target || '（这一段没有返回内容）' }}</p>
            <details v-if="s.source" class="mt-1">
              <summary class="cursor-pointer text-[10.5px] text-ink-4 hover:text-ink-2">原文</summary>
              <p class="mt-0.5 text-[11.5px] leading-relaxed whitespace-pre-wrap text-ink-3">{{ s.source }}</p>
            </details>
          </article>

          <p v-if="!segments.length" class="py-10 text-center text-2xs text-ink-4">
            还没有可对照的段落 —— 先翻译一次。
          </p>
        </div>
      </section>
    </div>
  </div>
</template>
