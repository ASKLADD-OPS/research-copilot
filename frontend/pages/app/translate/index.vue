<script setup lang="ts">
/**
 * 学术翻译：左原文 / 右译文，按段落对齐、滚动同步。
 *
 * 为什么同步要锚在**段落 ID** 而不是按比例：译文长度和原文差得多（中英字数比约 1:1.7），
 * 按 scrollTop 百分比算，滚到中段就开始漂，越往后偏得越离谱。后端切段时把 `p1/p2/…`
 * 定死并回传（见 app/api/v1/translate.py），前端只做一件事：把同 ID 的两段对齐到同一相对位置。
 *
 * 术语一致不靠提示词祈祷模型记住 —— 术语表整份抄进 prompt 并声明必须照译，
 * 返回的 `unused_terms` 告诉你哪几条压根没在原文出现（术语表配错时唯一的线索）。
 * 公式与引用编号的保全由后端提示词规则负责，前端不做二次加工，免得把 LaTeX 改坏。
 */
import {
  PhArrowLeft,
  PhArrowsClockwise,
  PhCopy,
  PhDownloadSimple,
  PhInfo,
  PhTranslate,
  PhUploadSimple,
  PhWarningCircle,
  PhX,
} from '@phosphor-icons/vue'
import type { GlossaryEntry, SourceBlock, TranslateParagraphsResult, TranslatedParagraph } from '~/types/api'

definePageMeta({ layout: false })

useHead({
  title: '学术翻译 · Research Copilot',
  meta: [{ name: 'robots', content: 'noindex, nofollow' }],
})

const api = useApi()
const library = useLibraryStore()

// ------------------------------------------------------------------ 视图
/** 纯文本（贴一段翻一段） / PDF 对照（左 PDF 右译文，滚动联动）。 */
const view = ref<'text' | 'pdf'>('text')

// ------------------------------------------------------------------ 参数
const target = ref<'en' | 'zh'>('en')
const keepTerms = ref(true)
const passive = ref(false)
const syncScroll = ref(true)

const TARGETS = [
  { value: 'en' as const, label: '中 → 英' },
  { value: 'zh' as const, label: '英 → 中' },
]

const VIEWS = [
  { value: 'text' as const, label: '纯文本' },
  { value: 'pdf' as const, label: 'PDF 对照' },
]

// ------------------------------------------------------------------ 术语表
const glossaryInput = ref<HTMLInputElement | null>(null)
const glossary = ref<GlossaryEntry[]>([])
const glossarySkipped = ref<string[]>([])
const glossaryBusy = ref(false)
const glossaryError = ref('')

async function pickGlossary(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  glossaryBusy.value = true
  glossaryError.value = ''
  try {
    const form = new FormData()
    form.append('file', file)
    const res = await api.upload<{ entries: GlossaryEntry[]; skipped: string[]; total_lines: number }>(
      '/translate/glossary',
      form,
    )
    glossary.value = res.entries
    glossarySkipped.value = res.skipped
  } catch (err) {
    glossaryError.value = (err as Error).message
  } finally {
    glossaryBusy.value = false
  }
}

function clearGlossary() {
  glossary.value = []
  glossarySkipped.value = []
}

// ------------------------------------------------------------------ 翻译
const source = ref('')
const translating = ref(false)
const translateError = ref('')
const result = ref<TranslateParagraphsResult | null>(null)

/** 后端字段上限 40000 —— 在输入框上就挡住，别让人敲完一整篇才被 422 打回来。 */
const MAX_CHARS = 40000
const sourceLength = computed(() => source.value.trim().length)

async function translate() {
  if (!source.value.trim() || translating.value) return
  translating.value = true
  translateError.value = ''
  result.value = null
  try {
    result.value = await api.post<TranslateParagraphsResult>('/translate', {
      text: source.value,
      target: target.value,
      glossary: glossary.value,
      keep_terms: keepTerms.value,
      passive: passive.value,
    })
  } catch (err) {
    translateError.value = (err as Error).message
  } finally {
    translating.value = false
  }
}

const paragraphs = computed(() => result.value?.paragraphs ?? [])

// ------------------------------------------------------------------ PDF 对照
/**
 * 把已入库论文的**检索块**直接当成段落送进 `/translate`。
 *
 * 为什么用块而不是正文全文：块自带 `page` + 归一化 `bbox`（见后端 `ChunkOut`），
 * 后端原样回传，前端才能把每段译文挂回 PDF 上的那个位置 —— 这是左右联动的全部依据。
 * 让前端自己按空行切全文会被迫再猜一次坐标，等于把已经算好的版面信息扔掉。
 */
const paperId = ref('')
const pairs = ref<TranslatedParagraph[]>([])
const pairBusy = ref(false)
const pairError = ref('')

const pdfUrl = computed(() => (paperId.value ? library.pdfUrl(paperId.value) : ''))
const readyPapers = computed(() => library.items.filter((p) => p.status === 'ready'))

async function buildPairs() {
  if (!paperId.value || pairBusy.value) return
  pairBusy.value = true
  pairError.value = ''
  pairs.value = []
  try {
    const chunks = await library.fetchChunks(paperId.value, 200)
    const blocks: SourceBlock[] = chunks
      .filter((c) => c.content?.trim())
      .map((c) => ({
        id: `c${c.id}`,
        text: c.content,
        page: c.page ?? c.page_start ?? null,
        bbox: c.bbox,
      }))
    if (!blocks.length) throw new Error('这篇论文还没有正文块 —— 等它解析完（状态「可检索」）再来')

    const res = await api.post<TranslateParagraphsResult>('/translate', {
      blocks,
      target: target.value,
      glossary: glossary.value,
      keep_terms: keepTerms.value,
      passive: passive.value,
    })
    pairs.value = res.paragraphs
  } catch (err) {
    pairError.value = (err as Error).message
  } finally {
    pairBusy.value = false
  }
}

onMounted(() => {
  if (!library.loaded) void library.load()
})

// ------------------------------------------------------------------ 滚动同步
/**
 * 一个"锁到什么时候"的时间戳，而不是布尔量。
 *
 * 程序化改动 `scrollTop` 会触发对侧容器的 scroll 事件，把两边连成回音室。
 * 用 rAF 解锁挡不住这个竞态（浏览器不保证 scroll 事件与 rAF 的先后），
 * 而 80ms 的时间窗足够长 —— 人手动滚一下的间隔不可能这么短，所以不会误吞真实操作。
 */
let lockUntil = 0
const leftPane = ref<HTMLElement | null>(null)
const rightPane = ref<HTMLElement | null>(null)

function onPaneScroll(from: 'source' | 'target') {
  if (!syncScroll.value || Date.now() < lockUntil) return
  const src = from === 'source' ? leftPane.value : rightPane.value
  const dst = from === 'source' ? rightPane.value : leftPane.value
  if (!src || !dst || !dst.querySelector('[data-pid]')) return
  const anchor = pickAnchor(src)
  if (!anchor) return
  const el = dst.querySelector<HTMLElement>(`[data-pid="${anchor.id}"]`)
  if (!el) return
  const want = el.offsetTop + anchor.ratio * el.offsetHeight
  if (Math.abs(want - dst.scrollTop) < 1) return // 已经对齐了，别白设一次去触发对侧的 scroll
  lockUntil = Date.now() + 80
  dst.scrollTop = want
}

/** 视口顶部落在哪一段里、落在这一段的百分之几 —— 用它去找对侧的同一位置。 */
function pickAnchor(pane: HTMLElement) {
  const els = Array.from(pane.querySelectorAll<HTMLElement>('[data-pid]'))
  if (!els.length) return null
  const top = pane.scrollTop
  const el = els.find((e) => e.offsetTop + e.offsetHeight > top + 1) ?? els[els.length - 1]!
  const pid = el.dataset.pid
  if (!pid) return null
  const ratio = Math.min(1, Math.max(0, (top - el.offsetTop) / Math.max(1, el.offsetHeight)))
  return { id: pid, ratio }
}

/** 段数差得多的两篇（比如原文有一段译文被并成一行）滚到某处会僵住，给个一键回顶。 */
function scrollToTop() {
  lockUntil = 0
  if (leftPane.value) leftPane.value.scrollTop = 0
  if (rightPane.value) rightPane.value.scrollTop = 0
}

// ------------------------------------------------------------------ 导出
async function copyAll() {
  if (!result.value) return
  try {
    await navigator.clipboard.writeText(result.value.target_text)
    copied.value = true
    setTimeout(() => (copied.value = false), 1600)
  } catch {
    translateError.value = '浏览器拒绝了剪贴板访问，请用「导出 .txt」'
  }
}

const copied = ref(false)

/**
 * 回到"输入原文"状态。写成函数而不是在模板里直接 `result = null`：
 * 模板里对 ref 赋值虽然能编译，但 `nuxt typecheck` 之后的可读性很差，
 * 而这一个动作后面还要接"保留术语表/清空译文"之类的清理，迟早要搬家。
 */
function reset() {
  result.value = null
  translateError.value = ''
}

/**
 * 导出为 .txt 而不是 .md：译文里常带 `#` 开头的公式或编号，当 Markdown 会被渲染坏。
 * 段落之间空一行，保持"一段一 ID"的对应关系能被人肉核对。
 */
function download() {
  if (!result.value) return
  const head = result.value.paragraphs.map((p) => p.target).join('\n\n')
  const blob = new Blob([head], { type: 'text/plain;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `translation-${result.value.language}-${Date.now()}.txt`
  a.click()
  URL.revokeObjectURL(a.href)
}
</script>

<template>
  <div class="flex h-screen flex-col bg-canvas text-[12.5px] text-ink">
    <!-- ---------------------------------------------------------------- 顶栏 -->
    <header class="flex h-11 shrink-0 items-center gap-2 border-b border-hairline bg-surface px-3">
      <NuxtLink
        to="/app"
        class="inline-flex h-6.5 shrink-0 items-center gap-1 rounded-md px-2 text-[11.5px] text-ink-3 transition-colors hover:bg-hover hover:text-ink"
      >
        <PhArrowLeft :size="11" />
        工作台
      </NuxtLink>
      <span class="mx-1 h-4 w-px shrink-0 bg-hairline" />
      <span class="grid size-5 shrink-0 place-items-center rounded-[6px] bg-brand text-white">
        <PhTranslate :size="12" weight="fill" />
      </span>
      <span class="text-[13px] font-semibold tracking-tight">学术翻译</span>

      <span v-if="result" :class="pillCls('default')">{{ paragraphs.length }} 段</span>
      <span v-if="result" :class="pillCls('brand')">{{ result.language === 'en' ? '中→英' : '英→中' }}</span>
      <!-- 术语表的语气分两种：还没翻译时无从判断命中，就不该冒充"生效"（这里 result 真的是 null） -->
      <span v-if="glossary.length" :class="pillCls(result ? (result.unused_terms.length ? 'warn' : 'ok') : 'default')">
        术语表 {{ glossary.length }} 条{{ result?.unused_terms.length ? ` · 未命中 ${result.unused_terms.length}` : '' }}
      </span>
      <span v-if="passive" :class="pillCls('default')">被动语态</span>

      <span class="min-w-0 flex-1" />

      <button
        v-if="result && paragraphs.length > 1"
        type="button"
        :class="btnCls('ghost', { size: 'sm' })"
        title="两栏回到顶部"
        @click="scrollToTop"
      >
        <PhArrowsClockwise :size="11" />
        回顶
      </button>
      <button
        v-if="result"
        type="button"
        :class="btnCls('ghost', { size: 'sm' })"
        title="复制译文全文"
        @click="copyAll"
      >
        <PhCopy :size="11" />
        {{ copied ? '已复制' : '复制译文' }}
      </button>
      <button
        v-if="result"
        type="button"
        :class="btnCls('ghost', { size: 'sm' })"
        title="导出译文（.txt，段落间空一行）"
        @click="download"
      >
        <PhDownloadSimple :size="11" />
        .txt
      </button>
    </header>

    <!-- ---------------------------------------------------------------- 参数条 -->
    <div class="flex shrink-0 flex-wrap items-center gap-2 border-b border-hairline bg-surface px-3 py-2">
      <Segmented v-model="target" :options="TARGETS" size="sm" aria-label="翻译方向" />

      <label class="inline-flex cursor-pointer items-center gap-1.5 text-2xs text-ink-2">
        <input v-model="passive" type="checkbox" class="size-3.5 accent-brand" />
        被动语态偏好
      </label>
      <label class="inline-flex cursor-pointer items-center gap-1.5 text-2xs text-ink-2">
        <input v-model="keepTerms" type="checkbox" class="size-3.5 accent-brand" />
        术语中英对照
      </label>
      <label class="inline-flex cursor-pointer items-center gap-1.5 text-2xs text-ink-2">
        <input v-model="syncScroll" type="checkbox" class="size-3.5 accent-brand" />
        滚动同步
      </label>

      <span class="mx-0.5 h-4 w-px shrink-0 bg-hairline" />

      <button type="button" :class="btnCls('default', { size: 'sm' })" :disabled="glossaryBusy" @click="glossaryInput?.click()">
        <AppSpinner v-if="glossaryBusy" :size="11" />
        <PhUploadSimple v-else :size="11" />
        术语表
      </button>
      <input ref="glossaryInput" type="file" accept=".csv,.tsv,.txt" class="hidden" @change="pickGlossary" />

      <span v-if="glossary.length" class="inline-flex items-center gap-1 text-2xs text-ink-3">
        {{ glossary.length }} 条生效
        <button
          type="button"
          class="grid size-4 place-items-center rounded-sm text-ink-4 hover:bg-hover hover:text-ink"
          title="清空术语表"
          @click="clearGlossary"
        >
          <PhX :size="10" />
        </button>
      </span>
      <span v-else-if="view === 'pdf'" class="text-2xs text-ink-4">
        选一篇已入库的论文 —— 段落按检索块切，页码与坐标由后端原样回传
      </span>
      <span v-else class="text-2xs text-ink-4">术语表：每行 <code class="font-mono">原文,译名</code></span>
    </div>

    <!-- PDF 对照：选文献 + 生成 -->
    <div
      v-if="view === 'pdf'"
      class="flex shrink-0 flex-wrap items-center gap-2 border-b border-hairline bg-surface px-3 py-2"
    >
      <select v-model="paperId" :class="SELECT_CLS" class="max-w-80">
        <option value="">选择文献…</option>
        <option v-for="p in readyPapers" :key="p.id" :value="p.id">{{ p.title || `#${p.id}` }}</option>
      </select>
      <button
        type="button"
        :class="btnCls('primary', { size: 'sm' })"
        :disabled="!paperId || pairBusy"
        @click="buildPairs"
      >
        <AppSpinner v-if="pairBusy" :size="11" />
        <PhTranslate v-else :size="11" weight="fill" />
        {{ pairBusy ? '正在逐块翻译…' : '生成对照' }}
      </button>
      <span v-if="pairs.length" :class="pillCls('brand')">{{ pairs.length }} 段已对齐</span>
      <span v-if="!readyPapers.length && library.loaded" class="text-2xs text-ink-4">
        文献库里还没有「可检索」的论文，先去工作台上传一篇
      </span>
      <p v-if="pairError" class="w-full rounded-md bg-bad-soft px-2 py-1 text-2xs text-bad">{{ pairError }}</p>
    </div>

    <p v-if="glossarySkipped.length" class="shrink-0 bg-warn-soft px-3 py-1.5 text-2xs leading-relaxed text-warn">
      <PhWarningCircle :size="11" class="mr-1 inline align-[-1px]" />
      术语表有 {{ glossarySkipped.length }} 行没认出来（没被采用）：{{ glossarySkipped.slice(0, 3).join(' / ') }}
      <template v-if="glossarySkipped.length > 3">…</template>
    </p>
    <p v-if="glossaryError" class="shrink-0 bg-bad-soft px-3 py-1.5 text-2xs text-bad">{{ glossaryError }}</p>

    <!-- ---------------------------------------------------------------- PDF 对照 -->
    <div v-if="view === 'pdf'" class="relative min-h-0 flex-1">
      <ClientOnly>
        <BilingualScroll
          v-if="pairs.length && pdfUrl"
          :segments="pairs"
          :paper-id="paperId"
          :pdf-url="pdfUrl"
        />
        <template #fallback>
          <div class="grid h-full place-items-center bg-sunken">
            <AppSpinner :size="18" class="text-ink-3" />
          </div>
        </template>
      </ClientOnly>

      <div v-if="!pairs.length" class="grid h-full place-items-center px-6">
        <div :class="EMPTY_CLS">
          <PhTranslate :size="22" class="mb-1.5 text-ink-4" />
          <strong class="text-[13px] text-ink-2">还没有对照数据</strong>
          <span class="max-w-80 text-2xs leading-relaxed text-ink-4">
            上面选一篇已入库的论文、点「生成对照」：正文按检索块逐块翻译，块自带的页码与归一化坐标
            一起回传，左栏 PDF 与右栏译文就能按段落互相定位（滚一侧，另一侧跟过去）。
          </span>
        </div>
      </div>
    </div>

    <!-- ---------------------------------------------------------------- 两栏 -->
    <div v-if="view === 'text' && !result" class="flex min-h-0 flex-1 flex-col gap-2 p-3">
      <div class="flex min-h-0 flex-1 flex-col gap-1.5">
        <div class="flex shrink-0 items-center gap-1.5">
          <span class="text-2xs font-semibold text-ink-2">原文</span>
          <span class="flex-1" />
          <span :class="pillCls(sourceLength > MAX_CHARS ? 'bad' : 'default')" class="tabular-nums">
            {{ sourceLength }} / {{ MAX_CHARS }}
          </span>
        </div>
        <textarea
          v-model="source"
          class="min-h-0 w-full flex-1 resize-none rounded-md border border-hairline-2 bg-surface px-3 py-2 font-mono text-[12.5px] leading-relaxed text-ink focus:border-brand focus:outline-none"
          placeholder="粘贴整篇原文。**空行分段** —— 段落边界决定了左右两栏怎么对齐，所以别把整篇挤成一行。公式（$…$）与引用编号（[1]）会原样保留。"
        />
      </div>

      <p v-if="translateError" class="shrink-0 rounded-md bg-bad-soft px-2 py-1.5 text-2xs text-bad">{{ translateError }}</p>

      <div class="flex shrink-0 items-center gap-2">
        <button
          type="button"
          :class="btnCls('primary', { block: true })"
          :disabled="translating || !source.trim() || sourceLength > MAX_CHARS"
          @click="translate"
        >
          <AppSpinner v-if="translating" :size="11" />
          <PhTranslate v-else :size="11" weight="fill" />
          {{ translating ? '正在逐段翻译…' : `翻译成${target === 'en' ? '英文' : '中文'}` }}
        </button>
      </div>

      <aside class="shrink-0 rounded-md border border-hairline bg-surface px-3 py-2">
        <p class="flex items-center gap-1.5 text-2xs font-semibold text-ink-2">
          <PhInfo :size="12" class="text-ink-4" />
          这一版怎么做的
        </p>
        <ul class="mt-1 space-y-0.5 text-2xs leading-relaxed text-ink-3">
          <li>· 按空行切段，逐段并发送模型（上限 4），**每段拿到一个固定 ID**（p1/p2/…），它就是两栏对齐的锚点。</li>
          <li>· 公式、变量名、引用编号（如 [1]）原样保留 —— 写在提示词规则里，逐段送进去能显著降低模型"顺手重排"的概率。</li>
          <li>· 术语表整份抄进 prompt 并声明必须照译；给完再回传**没命中的词条**，让你能核对一致性而不是靠感觉。</li>
          <li>· 被动语态偏好开启后，会要求无人称表述与被动结构（英文论文的常见写法）。</li>
        </ul>
      </aside>
    </div>

    <div v-else-if="view === 'text' && result" class="grid min-h-0 flex-1 grid-cols-2">
      <!-- 原文 -->
      <section class="flex min-h-0 flex-col border-r border-hairline">
        <div class="flex h-8 shrink-0 items-center gap-1.5 border-b border-hairline bg-surface px-3">
          <span class="text-2xs font-semibold text-ink-2">原文</span>
          <span :class="pillCls('default')">{{ paragraphs.length }} 段</span>
          <span class="flex-1" />
          <button type="button" :class="btnCls('ghost', { size: 'sm' })" title="改原文、重新翻译" @click="reset">
            重新输入
          </button>
        </div>
        <div
          ref="leftPane"
          class="relative min-h-0 flex-1 overflow-y-auto scroll-slim px-3 py-2"
          @scroll.passive="onPaneScroll('source')"
        >
          <div
            v-for="p in paragraphs"
            :key="p.id"
            :data-pid="p.id"
            class="mb-2 rounded-md px-2 py-1.5 transition-colors hover:bg-hover"
          >
            <span class="mb-0.5 block font-mono text-[10.5px] text-ink-4">{{ p.id }}</span>
            <p class="text-[12.5px] leading-relaxed whitespace-pre-wrap text-ink">{{ p.source }}</p>
          </div>
        </div>
      </section>

      <!-- 译文 -->
      <section class="flex min-h-0 flex-col">
        <div class="flex h-8 shrink-0 items-center gap-1.5 border-b border-hairline bg-surface px-3">
          <span class="text-2xs font-semibold text-ink-2">译文</span>
          <span :class="pillCls('brand')">{{ result.language === 'en' ? 'English' : '中文' }}</span>
          <span class="flex-1" />
          <span v-if="!syncScroll" :class="pillCls('warn')">同步已关</span>
        </div>
        <div
          ref="rightPane"
          class="relative min-h-0 flex-1 overflow-y-auto scroll-slim px-3 py-2"
          @scroll.passive="onPaneScroll('target')"
        >
          <div
            v-for="p in paragraphs"
            :key="p.id"
            :data-pid="p.id"
            class="mb-2 rounded-md border border-hairline bg-surface px-2 py-1.5"
          >
            <span class="mb-0.5 flex items-center gap-1.5">
              <span class="font-mono text-[10.5px] text-ink-4">{{ p.id }}</span>
              <span v-if="!p.target.trim()" :class="pillCls('warn')">空译文</span>
            </span>
            <p class="text-[12.5px] leading-relaxed whitespace-pre-wrap text-ink">{{ p.target || '（这一段没有返回内容）' }}</p>
          </div>

          <div v-if="result.unused_terms.length" class="mt-3 rounded-md bg-warn-soft px-2 py-1.5 text-2xs leading-relaxed text-warn">
            <PhWarningCircle :size="11" class="mr-1 inline align-[-1px]" />
            术语表里这 {{ result.unused_terms.length }} 条没在原文出现，等于没生效 ——
            多半是拼写或大小写对不上：{{ result.unused_terms.slice(0, 6).join(' / ') }}
            <template v-if="result.unused_terms.length > 6">…</template>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>
