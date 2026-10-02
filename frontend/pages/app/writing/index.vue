<script setup lang="ts">
/**
 * 写作台：左 Idea + 大纲 / 中 正文编辑器 / 右 引用 + 图表。
 *
 * 为什么要独立路由（而不是工作台右栏再加一个标签）：这条链路一次要跑六节检索 + 六次模型
 * 调用（十几秒到一分钟），中途还要在"大纲—正文—引用"三者之间来回看。右栏那 400px 宽
 * 装不下三栏，而把长任务塞进工作台会把"看着原文提问"的空间挤没 —— 与主题探索页同一个理由。
 *
 * 数据流与后端的对应关系：
 *   POST /writing/outline    → Idea 生成六节框架，**每节自带带引用的草稿**（全篇统一编号）
 *   POST /writing/expand     → 扩写编辑器选区；`marker_offset` 保证新编号接在文档末尾之后
 *   POST /writing/references → 校验本节引用；硬失败的编号被摘掉，正文原地替换
 *   POST /writing/diagram    → 生成图表；有渲染器就出图，没有就只给源码（如实告知）
 *
 * 编辑状态是**按节本地保存**的，所以正文可以直接改；「全文」视图只读，用来一次性导出。
 */
import {
  PhArrowLeft,
  PhArrowsClockwise,
  PhChartLine,
  PhCheckCircle,
  PhDownloadSimple,
  PhEye,
  PhListChecks,
  PhPencilLine,
  PhPlay,
  PhSelectionAll,
  PhShieldCheck,
  PhSparkle,
  PhWarningCircle,
} from '@phosphor-icons/vue'
import type {
  Citation,
  CitationCheck,
  DiagramKind,
  DiagramResult,
  ExpandResult,
  OutlineResult,
  OutlineSection,
  ReferenceResult,
  ReferenceStatus,
} from '~/types/api'

definePageMeta({ layout: false })

useHead({
  title: '写作台 · Research Copilot',
  meta: [{ name: 'robots', content: 'noindex, nofollow' }],
})

const api = useApi()
const library = useLibraryStore()

// ------------------------------------------------------------------ 大纲
const idea = ref('')
const lang = ref<'zh' | 'en'>('zh')
const evidencePerSection = ref(5)
const alsoDraft = ref(true)
const outlining = ref(false)
const outlineError = ref('')
const outline = ref<OutlineResult | null>(null)
/** 大纲的**本地可编辑副本**：正文改动不该退回服务端，也不该被重渲染冲掉。 */
const sections = ref<OutlineSection[]>([])
const activeIndex = ref(0)

const activeSection = computed<OutlineSection | null>(() => sections.value[activeIndex.value] ?? null)
const groundRatio = computed(() => outline.value?.grounding_ratio ?? 0)

/** 后端 `SectionKind` 里允许的章节名 —— 扩写接口只收这一组，其他 key 会 422。 */
const SECTION_KEYS = new Set([
  'abstract',
  'introduction',
  'related_work',
  'method',
  'experiment',
  'conclusion',
  'summary',
  'review',
  'rebuttal',
])

/** 校验结果按节存：切来切去不该把上一节的结论冲掉。 */
const checksBySection = ref<Record<string, CitationCheck[]>>({})

/** 文档里出现过的最大编号 —— 扩写时用它当偏移量，新编号不会和前面撞车。 */
const maxMarker = computed(() => {
  let top = 0
  for (const s of sections.value) {
    for (const c of s.citations) if (c.marker != null && c.marker > top) top = c.marker
    if (s.removed_markers.length) top = Math.max(top, ...s.removed_markers)
  }
  return top
})

/** 本节里通过校验的编号 —— 未通过的在预览里会被 MarkdownView 标红。 */
const verifiedMarkers = computed(
  () => new Set((activeSection.value?.citations ?? []).filter((c) => c.verified).map((c) => c.marker as number)),
)

async function generateOutline() {
  if (!idea.value.trim() || outlining.value) return
  outlining.value = true
  outlineError.value = ''
  try {
    const res = await api.post<OutlineResult>('/writing/outline', {
      idea: idea.value,
      paper_ids: library.scopePaperIds,
      language: lang.value,
      sections: [],
      evidence_per_section: evidencePerSection.value,
      draft: alsoDraft.value,
    })
    outline.value = res
    // 深拷贝一份可编辑副本：后面所有编辑都只动本地，重新生成时才整体覆盖
    sections.value = res.sections.map((s) => ({ ...s, points: [...s.points], citations: [...s.citations] }))
    activeIndex.value = 0
    checksBySection.value = {}
    view.value = res.sections.some((s) => s.draft) ? 'edit' : 'document'
  } catch (err) {
    outlineError.value = (err as Error).message
  } finally {
    outlining.value = false
  }
}

// ------------------------------------------------------------------ 编辑器
const view = ref<'edit' | 'preview' | 'document'>('edit')
const editorEl = ref<HTMLTextAreaElement | null>(null)
const VIEWS = [
  { value: 'edit' as const, label: '编辑', icon: PhPencilLine },
  { value: 'preview' as const, label: '预览', icon: PhEye },
  { value: 'document' as const, label: '全文', icon: PhListChecks },
]

const documentText = computed(() => {
  if (!sections.value.length) return ''
  const head = outline.value?.title ? `# ${outline.value.title}\n\n` : ''
  const body = sections.value.map((s) => `## ${s.title}\n\n${s.draft || '（本节尚未生成正文）'}`).join('\n\n')
  const refs = outline.value?.references ?? []
  const bib = refs.length ? `\n\n## References\n\n${refs.map((r) => r.formatted).join('\n\n')}` : ''
  return head + body + bib
})

/** 当前选区；没选中就返回本节全文（按钮上写清楚这一点，别让人猜）。 */
function currentSelection(): { start: number; end: number; text: string } {
  const draft = activeSection.value?.draft ?? ''
  const el = editorEl.value
  if (el && el.selectionStart !== el.selectionEnd) {
    const start = Math.min(el.selectionStart, el.selectionEnd)
    const end = Math.max(el.selectionStart, el.selectionEnd)
    return { start, end, text: draft.slice(start, end) }
  }
  return { start: 0, end: draft.length, text: draft }
}

/**
 * 正文改动只写回本地副本。
 *
 * 不用 `v-model="(activeSection as OutlineSection).draft"`：Vue 明确拒绝在类型断言表达式上
 * 用 v-model（"v-model cannot be used on a type cast expression"），编译期就报错。
 */
function onDraftInput(e: Event) {
  const sec = activeSection.value
  if (sec) sec.draft = (e.target as HTMLTextAreaElement).value
}

// ------------------------------------------------------------------ 扩写
const expandInstruction = ref('')
const expanding = ref(false)
const actionError = ref('')
const lastNote = ref('')

async function expandSelection() {
  const sec = activeSection.value
  if (!sec || expanding.value) return
  const picked = currentSelection()
  if (!picked.text.trim()) {
    actionError.value = '本节还没有正文可扩写 —— 先生成大纲，或手动写一句要点。'
    return
  }
  expanding.value = true
  actionError.value = ''
  // 偏移量必须在合并 citations **之前**取：合并后 maxMarker 就变成新的最大值了
  const offset = maxMarker.value
  try {
    const res = await api.post<ExpandResult>('/writing/expand', {
      text: picked.text,
      context: sec.draft,
      instruction: expandInstruction.value,
      section: sec.key in SECTION_KEYS || SECTION_KEYS.has(sec.key) ? sec.key : 'related_work',
      language: lang.value,
      paper_ids: library.scopePaperIds,
      query: '',
      target_length: null,
      evidence_top_k: 8,
      // 接在文档已有编号之后：不这样做，插入进来的 [1] 会和文档开头的 [1] 撞号
      marker_offset: offset,
    })
    const draft = sec.draft
    sec.draft = draft.slice(0, picked.start) + res.text + draft.slice(picked.end)
    sec.citations = [...sec.citations, ...res.citations]
    lastNote.value =
      `扩写完成：${res.text.length} 字 · 新增引用 ${res.citations.length} 条（编号从 ${offset + 1} 起）`
      + (res.removed_markers.length ? ` · 摘掉幻觉编号 ${res.removed_markers.join('、')}` : '')
  } catch (err) {
    actionError.value = (err as Error).message
  } finally {
    expanding.value = false
  }
}

// ------------------------------------------------------------------ 引用校验
const checking = ref(false)
const removeInvalid = ref(true)
const lastBibliography = ref<string[]>([])
const activeChecks = computed<CitationCheck[]>(() =>
  activeSection.value ? (checksBySection.value[activeSection.value.key] ?? []) : [],
)

async function checkSection() {
  const sec = activeSection.value
  if (!sec || checking.value) return
  if (!sec.draft.trim()) {
    actionError.value = '本节没有正文可校验。'
    return
  }
  checking.value = true
  actionError.value = ''
  try {
    const res = await api.post<ReferenceResult>('/writing/references', {
      content: sec.draft,
      query: outline.value?.title || idea.value,
      paper_ids: library.scopePaperIds,
      // 回传写正文那一步的 citations：编号映射不用重新猜，宁可多传这几个字段
      citations: sec.citations as unknown as Record<string, unknown>[],
      top_k: 20,
      remove_invalid: removeInvalid.value,
      language: lang.value,
    })
    sec.draft = res.content
    // 只留下还能排进参考文献表的编号，预览里的角标颜色才和校验结论一致
    const survived = new Set(
      res.checks.filter((c) => c.status === 'ok' || c.status === 'weak').map((c) => c.marker),
    )
    sec.citations = sec.citations.filter((c) => c.marker != null && survived.has(c.marker))
    checksBySection.value = { ...checksBySection.value, [sec.key]: res.checks }
    if (res.bibliography.length) lastBibliography.value = res.bibliography
    lastNote.value =
      `本节校验：${res.total_markers} 条引用 → 通过 ${res.ok_count}、待核对 ${res.flagged_markers.length}、`
      + `已处置 ${res.invalid_count}${res.removed_markers.length ? `（编号 ${res.removed_markers.join('、')}）` : ''}`
  } catch (err) {
    actionError.value = (err as Error).message
  } finally {
    checking.value = false
  }
}

const CHECK_LABEL: Record<ReferenceStatus, string> = {
  ok: '通过',
  weak: '待核对',
  phantom: '幻觉编号',
  chunk_missing: '证据缺失',
  metadata_missing: '元数据缺失',
}

function checkTone(status: ReferenceStatus) {
  if (status === 'ok') return 'ok' as const
  if (status === 'weak') return 'warn' as const
  return 'bad' as const
}

// ------------------------------------------------------------------ 图表
const rightTab = ref<'citations' | 'diagram'>('citations')
const diagramKind = ref<DiagramKind>('mermaid')
const diagramInstruction = ref('')
const diagram = ref<DiagramResult | null>(null)
const drawing = ref(false)

const DIAGRAM_KINDS = [
  { value: 'mermaid' as const, label: 'Mermaid' },
  { value: 'graphviz' as const, label: 'Graphviz' },
  { value: 'tikz' as const, label: 'TikZ' },
  { value: 'matplotlib' as const, label: 'Matplotlib' },
]

const KIND_HINT: Record<DiagramKind, string> = {
  mermaid: '流程图，前端/文档里都渲染得出来',
  graphviz: 'DOT 语言，装没装 Graphviz 都能出图（没装就是拓扑草图）',
  tikz: 'LaTeX 架构图，直接贴进论文 .tex 编译',
  matplotlib: '数据图表，数字只能来自材料，不给数据就不画',
}

const FENCE_LANG: Record<DiagramKind, string> = {
  tikz: 'latex',
  graphviz: 'dot',
  mermaid: 'mermaid',
  matplotlib: 'json',
}

const imageSrc = computed(() => (diagram.value?.image ? `data:image/png;base64,${diagram.value.image}` : ''))

async function makeDiagram() {
  const sec = activeSection.value
  if (drawing.value) return
  const instruction = diagramInstruction.value.trim() || (sec ? `${sec.title}：${sec.brief || '系统架构'}` : '')
  if (!instruction) {
    actionError.value = '先写一句"要画什么"。'
    return
  }
  drawing.value = true
  actionError.value = ''
  diagram.value = null
  try {
    diagram.value = await api.post<DiagramResult>('/writing/diagram', {
      kind: diagramKind.value,
      instruction,
      // 把本节要点当材料：图里的模块名要来自正文，不能凭空冒出来
      context: sec ? `${sec.title}\n${sec.brief}\n${(sec.points ?? []).join('；')}\n${sec.draft.slice(0, 1200)}` : '',
      data: null,
    })
  } catch (err) {
    actionError.value = (err as Error).message
  } finally {
    drawing.value = false
  }
}

/**
 * 插入策略按"能不能贴进论文"分：
 * - TikZ / DOT / Mermaid 插**源码**（它们是可编辑的资源，PNG 贴进稿子就成死图了）；
 * - Matplotlib 的源码只是一份 JSON 规格，对读者没意义，插 PNG（data URI）。
 */
function insertDiagram() {
  const sec = activeSection.value
  const d = diagram.value
  if (!sec || !d) return
  const caption = d.caption ? `\n\n${d.caption}` : ''
  const block =
    d.kind === 'matplotlib' && d.image
      ? `\n\n![${d.caption || 'figure'}](data:${d.mime};base64,${d.image})${caption}`
      : `\n\n\`\`\`${FENCE_LANG[d.kind]}\n${d.source}\n\`\`\`${caption}`
  sec.draft = `${sec.draft.trimEnd()}${block}`
  lastNote.value = `已插入 ${d.kind} 图到「${sec.title}」末尾（渲染器 ${d.renderer}）`
  view.value = 'preview'
}

// ------------------------------------------------------------------ 导出
function download() {
  const blob = new Blob([documentText.value], { type: 'text/markdown;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `paper-${Date.now()}.md`
  a.click()
  URL.revokeObjectURL(a.href)
}

async function copyAll() {
  try {
    await navigator.clipboard.writeText(documentText.value)
    lastNote.value = '全文已复制到剪贴板'
  } catch {
    lastNote.value = '浏览器拒绝了剪贴板访问，请用「导出 .md」'
  }
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
        <PhSparkle :size="12" weight="fill" />
      </span>
      <span class="text-[13px] font-semibold tracking-tight">写作台</span>

      <span v-if="outline" :class="pillCls(groundingTone(groundRatio))">
        全篇有据率 {{ (groundRatio * 100).toFixed(0) }}%
      </span>
      <span v-if="outline" :class="pillCls('default')">{{ sections.length }} 节</span>
      <span v-if="outline" :class="pillCls('brand')">{{ outline.references.length }} 篇参考文献</span>
      <span v-if="outline?.phantom_markers.length" :class="pillCls('bad')">
        已清除 {{ outline.phantom_markers.length }} 个幻觉编号
      </span>

      <span class="min-w-0 flex-1" />

      <span v-if="lastNote" class="max-w-[46ch] truncate text-2xs text-ink-3" :title="lastNote">
        {{ lastNote }}
      </span>
      <span class="min-w-0 flex-1" />

      <button
        v-if="outline"
        type="button"
        :class="btnCls('ghost', { size: 'sm' })"
        title="复制全文（含 References）"
        @click="copyAll"
      >
        <PhSelectionAll :size="11" />
        复制
      </button>
      <button
        v-if="outline"
        type="button"
        :class="btnCls('ghost', { size: 'sm' })"
        title="导出 Markdown（含 References）"
        @click="download"
      >
        <PhDownloadSimple :size="11" />
        .md
      </button>
    </header>

    <div class="grid min-h-0 flex-1 grid-cols-[300px_1fr_340px]">
      <!-- ------------------------------------------------------------ 左：Idea + 大纲 -->
      <aside class="flex min-h-0 flex-col border-r border-hairline bg-surface">
        <div class="shrink-0 space-y-2.5 border-b border-hairline px-3 py-3">
          <div :class="FIELD_CLS">
            <label :class="LABEL_CLS" for="wr-idea">Idea</label>
            <textarea
              id="wr-idea"
              v-model="idea"
              :class="TEXTAREA_CLS"
              rows="5"
              placeholder="如：用稀疏路由降低 MoE 推理开销，并在长文本任务上验证"
              @keydown.meta.enter="generateOutline"
              @keydown.ctrl.enter="generateOutline"
            />
          </div>

          <div class="grid grid-cols-2 gap-2">
            <div :class="FIELD_CLS">
              <label :class="LABEL_CLS" for="wr-lang">语言</label>
              <select id="wr-lang" v-model="lang" :class="SELECT_CLS">
                <option value="zh">中文</option>
                <option value="en">English</option>
              </select>
            </div>
            <div :class="FIELD_CLS">
              <label :class="LABEL_CLS" for="wr-topk">每节证据</label>
              <input id="wr-topk" v-model.number="evidencePerSection" :class="INPUT_CLS" type="number" min="1" max="20" />
            </div>
          </div>

          <label class="flex cursor-pointer items-start gap-2 text-2xs leading-relaxed text-ink-2">
            <input v-model="alsoDraft" type="checkbox" class="mt-0.5 size-3.5 accent-brand" />
            同时逐节起草（关闭只出框架，不检索也不排参考文献）
          </label>

          <p class="text-2xs text-ink-4">
            证据范围：{{ library.scopeNarrowed ? `限定 ${library.scopePaperIds.length} 篇` : '全库' }}
            · 在文献库面板里调整
          </p>

          <button
            type="button"
            :class="btnCls('primary', { block: true })"
            :disabled="outlining || !idea.trim()"
            @click="generateOutline"
          >
            <AppSpinner v-if="outlining" :size="11" />
            <PhSparkle v-else :size="11" weight="fill" />
            {{ alsoDraft ? '生成论文框架' : '只生成大纲' }}
          </button>

          <p v-if="outlining" class="text-2xs leading-relaxed text-ink-4">
            正在逐节检索并起草（{{ sections.length || 6 }} 节，每节一次检索 + 一次生成）——
            这通常要十几秒到一分钟。
          </p>
          <p v-if="outlineError" class="rounded-md bg-bad-soft px-2 py-1.5 text-2xs text-bad">{{ outlineError }}</p>
        </div>

        <!-- 大纲列表 -->
        <div class="min-h-0 flex-1 overflow-y-auto scroll-slim px-2 py-2">
          <div v-if="!sections.length" :class="EMPTY_CLS">
            <strong class="text-[12.5px] text-ink-2">还没有大纲</strong>
            <span class="text-2xs leading-relaxed text-ink-4">
              填 Idea 后点「生成论文框架」。每节会单独检索证据，正文里的 [n] 可点开核对原文。
            </span>
          </div>

          <ul v-else class="space-y-1">
            <li v-for="(s, i) in sections" :key="s.key">
              <button
                type="button"
                class="w-full rounded-md px-2 py-1.5 text-left transition-colors"
                :class="i === activeIndex ? 'bg-hover' : 'hover:bg-hover'"
                @click="activeIndex = i"
              >
                <span class="flex items-center gap-1.5">
                  <span class="min-w-0 flex-1 truncate text-[12.5px] font-medium text-ink">{{ s.title }}</span>
                  <span v-if="s.citations.length" :class="pillCls(groundingTone(s.grounding_ratio))">
                    {{ (s.grounding_ratio * 100).toFixed(0) }}%
                  </span>
                </span>
                <span class="mt-0.5 flex flex-wrap items-center gap-1">
                  <span v-if="s.evidence_count" :class="pillCls('default')">{{ s.evidence_count }} 块证据</span>
                  <span v-if="s.draft" :class="pillCls('default')">{{ s.draft.length }} 字</span>
                  <span v-if="s.removed_markers.length" :class="pillCls('bad')">
                    清除 {{ s.removed_markers.length }} 处幻觉引用
                  </span>
                </span>
                <span v-if="s.brief" class="mt-1 line-clamp-2 block text-2xs leading-relaxed text-ink-4">
                  {{ s.brief }}
                </span>
              </button>
            </li>
          </ul>
        </div>
      </aside>

      <!-- ------------------------------------------------------------ 中：正文 -->
      <main class="flex min-h-0 flex-col border-r border-hairline">
        <div :class="TOOLBAR_CLS">
          <Segmented v-model="view" :options="VIEWS" size="sm" aria-label="正文视图" />

          <span class="min-w-0 flex-1 truncate px-1 text-[12px] font-medium text-ink-2">
            {{ activeSection?.title ?? '—' }}
          </span>

          <input
            v-if="view === 'edit'"
            v-model="expandInstruction"
            :class="INPUT_CLS"
            class="max-w-[240px] shrink"
            placeholder="扩写要求（可选）"
          />
          <button
            type="button"
            :class="btnCls('default', { size: 'sm' })"
            :disabled="expanding || !activeSection"
            title="选中要扩写的段落；没选中就扩写全节"
            @click="expandSelection"
          >
            <AppSpinner v-if="expanding" :size="11" />
            <PhArrowsClockwise v-else :size="11" />
            扩写
          </button>
          <button
            type="button"
            :class="btnCls('default', { size: 'sm' })"
            :disabled="checking || !activeSection"
            title="校验本节引用：chunk 是否存在 / 语义是否被支持 / 元数据是否齐全"
            @click="checkSection"
          >
            <AppSpinner v-if="checking" :size="11" />
            <PhShieldCheck v-else :size="11" />
            校验引用
          </button>
        </div>

        <p v-if="actionError" class="shrink-0 bg-bad-soft px-3 py-1.5 text-2xs text-bad">{{ actionError }}</p>

        <div class="min-h-0 flex-1 overflow-y-auto scroll-slim p-3">
          <div v-if="!sections.length" :class="EMPTY_CLS">
            <strong class="text-[12.5px] text-ink-2">正文区是空的</strong>
            <span class="text-2xs leading-relaxed text-ink-4">
              先在左侧生成大纲。生成后这里就是可编辑的 Markdown 编辑器（公式用 $…$，预览由 MathJax 排版）。
            </span>
          </div>

          <template v-else-if="view === 'edit'">
            <textarea
              ref="editorEl"
              v-model="(activeSection as OutlineSection).draft"
              class="w-full resize-none rounded-md border border-hairline-2 bg-surface px-3 py-2 font-mono text-[12.5px] leading-relaxed text-ink focus:border-brand focus:outline-none"
              :style="{ minHeight: '60vh' }"
              spellcheck="false"
              placeholder="本节正文（Markdown）。选中一段后点「扩写」，或点「校验引用」逐条核对。"
            />
            <p class="mt-2 text-2xs leading-relaxed text-ink-4">
              选中一段 → 扩写只替换选区；未选中则扩写全节。新引用编号会接在文档现有编号之后。
            </p>
          </template>

          <MarkdownView
            v-else-if="view === 'preview'"
            :source="activeSection?.draft || ''"
            :verified="verifiedMarkers"
          />

          <MarkdownView v-else :source="documentText" />
        </div>
      </main>

      <!-- ------------------------------------------------------------ 右：引用 + 图表 -->
      <aside class="flex min-h-0 flex-col bg-surface">
        <div class="shrink-0 border-b border-hairline px-2.5 py-1.5">
          <Segmented
            v-model="rightTab"
            :options="[
              { value: 'citations', label: '引用', icon: PhListChecks },
              { value: 'diagram', label: '图表', icon: PhChartLine },
            ]"
            size="sm"
            aria-label="右栏标签"
          />
        </div>

        <!-- 引用面板 -->
        <div v-if="rightTab === 'citations'" class="min-h-0 flex-1 space-y-3 overflow-y-auto scroll-slim px-2.5 py-2.5">
          <section v-if="activeSection">
            <p class="mb-1 flex items-center gap-1.5 text-2xs font-semibold text-ink-2">
              本节引用
              <span v-if="activeSection.citations.length" :class="pillCls(groundingTone(activeSection.grounding_ratio))">
                {{ (activeSection.grounding_ratio * 100).toFixed(0) }}% 有据
              </span>
            </p>
            <CitationList v-if="activeSection.citations.length" :citations="activeSection.citations" compact />
            <p v-else class="text-2xs text-ink-4">本节没有引用 —— 开「先检索证据」再生成，或点上方「校验引用」。</p>
          </section>

          <section v-if="activeChecks.length">
            <p class="mb-1 text-2xs font-semibold text-ink-2">逐条校验</p>
            <ul class="space-y-1">
              <li
                v-for="c in activeChecks"
                :key="c.marker"
                class="rounded-md border border-hairline px-2 py-1.5"
              >
                <span class="flex items-center gap-1.5">
                  <span class="cite-mark shrink-0">{{ c.marker }}</span>
                  <span :class="pillCls(checkTone(c.status))">{{ CHECK_LABEL[c.status] }}</span>
                  <span v-if="c.nli_score" :class="pillCls('default')">蕴含 {{ c.nli_score.toFixed(2) }}</span>
                </span>
                <p class="mt-1 text-2xs leading-relaxed text-ink-3">{{ c.reason }}</p>
                <p v-if="c.claim" class="mt-1 line-clamp-2 text-2xs leading-relaxed text-ink-4">{{ c.claim }}</p>
              </li>
            </ul>
          </section>

          <section v-if="lastBibliography.length">
            <p class="mb-1 text-2xs font-semibold text-ink-2">上次校验的参考文献表</p>
            <ol class="space-y-1">
              <li v-for="b in lastBibliography" :key="b" class="text-2xs leading-relaxed text-ink-3">{{ b }}</li>
            </ol>
          </section>

          <section v-if="outline?.references.length">
            <p class="mb-1 text-2xs font-semibold text-ink-2">
              全篇参考文献（{{ outline.references.length }} 条，全部来自元数据）
            </p>
            <ol class="space-y-1">
              <li v-for="r in outline.references" :key="`${r.marker}-${r.chunk_id}`" class="text-2xs leading-relaxed text-ink-3">
                {{ r.formatted }}
              </li>
            </ol>
          </section>

          <label v-if="activeChecks.length" class="flex cursor-pointer items-start gap-2 text-2xs leading-relaxed text-ink-2">
            <input v-model="removeInvalid" type="checkbox" class="mt-0.5 size-3.5 accent-brand" />
            校验失败时直接摘掉编号（关闭则原地留 [citation needed]，方便回头补文献）
          </label>

          <div v-if="!activeSection && !outline" :class="EMPTY_CLS">
            <strong class="text-[12.5px] text-ink-2">还没有引用</strong>
            <span class="text-2xs leading-relaxed text-ink-4">生成大纲后，每节的引用与校验结果都会出现在这里。</span>
          </div>
        </div>

        <!-- 图表面板 -->
        <div v-else class="min-h-0 flex-1 space-y-2.5 overflow-y-auto scroll-slim px-2.5 py-2.5">
          <Segmented v-model="diagramKind" :options="DIAGRAM_KINDS" size="sm" aria-label="图表类型" />
          <p class="text-2xs leading-relaxed text-ink-4">{{ KIND_HINT[diagramKind] }}</p>

          <div :class="FIELD_CLS">
            <label :class="LABEL_CLS" for="wr-diagram">要画什么</label>
            <textarea
              id="wr-diagram"
              v-model="diagramInstruction"
              :class="TEXTAREA_CLS"
              rows="3"
              placeholder="留空则用当前章节的标题与要点"
            />
          </div>

          <button type="button" :class="btnCls('primary', { block: true })" :disabled="drawing" @click="makeDiagram">
            <AppSpinner v-if="drawing" :size="11" />
            <PhPlay v-else :size="11" weight="fill" />
            生成图表
          </button>

          <template v-if="diagram">
            <p class="flex flex-wrap items-center gap-1.5">
              <span :class="pillCls(diagram.image ? 'ok' : 'warn')">
                {{ diagram.image ? `已出图 · ${diagram.renderer}` : `仅源码 · ${diagram.renderer}` }}
              </span>
              <span :class="pillCls('default')">{{ diagram.elapsed_ms }} ms</span>
            </p>

            <p v-if="diagram.warning" class="rounded-md bg-warn-soft px-2 py-1.5 text-2xs leading-relaxed text-warn">
              <PhWarningCircle :size="11" class="mr-1 inline align-[-1px]" />
              {{ diagram.warning }}
            </p>

            <img
              v-if="imageSrc"
              :src="imageSrc"
              :alt="diagram.caption || '生成的图表'"
              class="w-full rounded-md border border-hairline bg-white"
            />
            <pre
              v-if="!imageSrc || diagram.kind !== 'matplotlib'"
              class="max-h-64 overflow-auto scroll-slim rounded-md bg-sunken px-2 py-1.5 font-mono text-[11px] leading-relaxed text-ink-2"
            >{{ diagram.source }}</pre>

            <p v-if="diagram.caption" class="text-2xs leading-relaxed text-ink-3">{{ diagram.caption }}</p>

            <button
              type="button"
              :class="btnCls('default', { block: true })"
              :disabled="!activeSection"
              :title="`插入到「${activeSection?.title ?? '—'}」末尾`"
              @click="insertDiagram"
            >
              <PhCheckCircle :size="11" />
              插入到正文
            </button>
            <p class="text-2xs leading-relaxed text-ink-4">
              TikZ / DOT / Mermaid 插入源码（可继续改），Matplotlib 插入 PNG（贴进稿子就能用）。
            </p>
          </template>

          <div v-else-if="!drawing" :class="EMPTY_CLS">
            <strong class="text-[12.5px] text-ink-2">还没有图</strong>
            <span class="text-2xs leading-relaxed text-ink-4">
              选一种类型，点「生成图表」。缺渲染器时会如实降级：能出图就出图，不能就只给源码。
            </span>
          </div>
        </div>
      </aside>
    </div>
  </div>
</template>
