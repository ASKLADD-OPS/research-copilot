<script setup lang="ts">
/**
 * 写作面板（纯内容）：章节草稿 + 学术翻译。
 *
 * 与对话的区别是"有据生成"被显式化：写之前先按主题检索证据，生成后再过一遍
 * 溯源引擎，所以结果区一定同时给出有据率与引用列表 —— 否则用户没法判断
 * 这段文字里哪些是真的有出处。
 */
import { PhDownloadSimple, PhPencilLine, PhTranslate } from '@phosphor-icons/vue'
import type { Citation, TranslateResult, WriteResult, WritingTemplate } from '~/types/api'

const api = useApi()
const library = useLibraryStore()

const MODES = [
  { value: 'draft' as const, label: '章节草稿', icon: PhPencilLine },
  { value: 'translate' as const, label: '翻译', icon: PhTranslate },
]

const tab = ref<'draft' | 'translate'>('draft')

// ---------------- 草稿 ----------------
const templates = ref<WritingTemplate[]>([])
const form = reactive({
  kind: 'summary',
  topic: '',
  language: 'zh' as 'zh' | 'en',
  target_length: undefined as number | undefined,
  style: '',
  use_retrieval: true,
})

const drafting = ref(false)
const draft = ref<WriteResult | null>(null)
const draftError = ref('')

const currentTemplate = computed(() => templates.value.find((t) => t.kind === form.kind))

async function loadTemplates() {
  try {
    templates.value = await api.get<WritingTemplate[]>('/writing/templates')
    if (templates.value.length && !templates.value.some((t) => t.kind === form.kind)) {
      form.kind = templates.value[0].kind
    }
  } catch (err) {
    draftError.value = (err as Error).message
  }
}

// 换模板时自动带上它的建议字数，省得用户每换一次都要重填
watch(
  () => form.kind,
  () => {
    const t = currentTemplate.value
    if (t) form.target_length = t.default_length
  },
)

async function generate() {
  if (!form.topic.trim()) return
  drafting.value = true
  draftError.value = ''
  draft.value = null
  try {
    draft.value = await api.post<WriteResult>('/writing/draft', {
      kind: form.kind,
      topic: form.topic,
      paper_ids: library.scopePaperIds,
      language: form.language,
      target_length: form.target_length ?? null,
      style: form.style,
      use_retrieval: form.use_retrieval,
    })
  } catch (err) {
    draftError.value = (err as Error).message
  } finally {
    drafting.value = false
  }
}

/** 写作接口返回的是原始 dict 引用，形状与 Citation 一致。 */
const draftCitations = computed<Citation[]>(() => (draft.value?.citations ?? []) as unknown as Citation[])

const verifiedMarkers = computed(
  () => new Set(draftCitations.value.filter((c) => c.verified && c.marker != null).map((c) => c.marker!)),
)

function downloadDraft() {
  if (!draft.value) return
  const blob = new Blob([draft.value.content], { type: 'text/markdown;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `${form.kind}-${Date.now()}.md`
  a.click()
  URL.revokeObjectURL(a.href)
}

// ---------------- 翻译 ----------------
const tr = reactive({ text: '', target: 'zh' as 'zh' | 'en', keep_terms: true, bilingual: false })
const translating = ref(false)
const trResult = ref<TranslateResult | null>(null)
const trError = ref('')

async function translate() {
  if (!tr.text.trim()) return
  translating.value = true
  trError.value = ''
  trResult.value = null
  try {
    trResult.value = await api.post<TranslateResult>('/writing/translate', { ...tr })
  } catch (err) {
    trError.value = (err as Error).message
  } finally {
    translating.value = false
  }
}

onMounted(() => void loadTemplates())
</script>

<template>
  <div class="flex h-full flex-col">
    <div class="shrink-0 border-b border-hairline px-2.5 py-1.5">
      <Segmented
        :model-value="tab"
        :options="MODES"
        size="sm"
        aria-label="写作模式"
        @update:model-value="tab = $event"
      />
    </div>

    <div class="min-h-0 flex-1 space-y-2.5 overflow-y-auto scroll-slim px-2.5 py-2.5">
      <template v-if="tab === 'draft'">
        <label :class="FIELD_CLS">
          <span :class="LABEL_CLS">模板</span>
          <select v-model="form.kind" :class="SELECT_CLS">
            <option v-for="t in templates" :key="t.kind" :value="t.kind">{{ t.name }}</option>
          </select>
        </label>

        <div v-if="currentTemplate" class="rounded-md bg-sunken px-2 py-1.5">
          <p class="text-2xs leading-relaxed text-ink-3">{{ currentTemplate.description }}</p>
          <div class="mt-1 flex flex-wrap gap-1">
            <span v-for="(o, i) in currentTemplate.outline" :key="o" :class="pillCls('default')">
              {{ i + 1 }}. {{ o }}
            </span>
          </div>
        </div>

        <label :class="FIELD_CLS">
          <span :class="LABEL_CLS">写什么</span>
          <textarea
            v-model="form.topic"
            :class="TEXTAREA_CLS"
            rows="4"
            placeholder="例如：对比这三篇论文在检索增强方法上的差异，指出各自的适用边界"
          />
        </label>

        <div class="grid grid-cols-2 gap-2">
          <label :class="FIELD_CLS">
            <span :class="LABEL_CLS">语言</span>
            <select v-model="form.language" :class="SELECT_CLS">
              <option value="zh">中文</option>
              <option value="en">English</option>
            </select>
          </label>
          <label :class="FIELD_CLS">
            <span :class="LABEL_CLS">目标字数</span>
            <input
              v-model.number="form.target_length"
              :class="INPUT_CLS"
              class="tabular-nums"
              type="number"
              min="100"
              max="8000"
              step="100"
            />
          </label>
        </div>

        <label :class="FIELD_CLS">
          <span :class="LABEL_CLS">风格要求</span>
          <input v-model="form.style" :class="INPUT_CLS" placeholder="如：学术、克制、避免口语与排比" />
        </label>

        <label class="flex cursor-pointer items-start gap-2 text-2xs leading-relaxed text-ink-2">
          <input v-model="form.use_retrieval" type="checkbox" class="mt-0.5 size-3.5 accent-brand" />
          先检索证据再写（关闭则纯生成，不再有引用与有据率）
        </label>

        <p class="text-2xs text-ink-4">
          证据范围：{{ library.scopeNarrowed ? `限定 ${library.scopePaperIds.length} 篇` : '全库' }}
          · 在文献库面板里调整
        </p>

        <button
          type="button"
          :class="btnCls('primary', { block: true })"
          :disabled="drafting || !form.topic.trim()"
          @click="generate"
        >
          <AppSpinner v-if="drafting" :size="11" />
          生成草稿
        </button>

        <p v-if="draftError" class="rounded-md bg-bad-soft px-2 py-1.5 text-2xs text-bad">{{ draftError }}</p>
      </template>

      <template v-else>
        <label :class="FIELD_CLS">
          <span :class="LABEL_CLS">原文</span>
          <textarea
            v-model="tr.text"
            :class="TEXTAREA_CLS"
            rows="10"
            placeholder="粘贴要翻译的段落，可多段（空行分段）"
          />
        </label>

        <label :class="FIELD_CLS">
          <span :class="LABEL_CLS">目标语言</span>
          <select v-model="tr.target" :class="SELECT_CLS">
            <option value="zh">中文</option>
            <option value="en">English</option>
          </select>
        </label>

        <label class="flex cursor-pointer items-center gap-2 text-2xs text-ink-2">
          <input v-model="tr.keep_terms" type="checkbox" class="size-3.5 accent-brand" />
          保留术语原文（括号内给译名）
        </label>
        <label class="flex cursor-pointer items-center gap-2 text-2xs text-ink-2">
          <input v-model="tr.bilingual" type="checkbox" class="size-3.5 accent-brand" />
          逐段对照输出
        </label>

        <button
          type="button"
          :class="btnCls('primary', { block: true })"
          :disabled="translating || !tr.text.trim()"
          @click="translate"
        >
          <AppSpinner v-if="translating" :size="11" />
          翻译
        </button>

        <p v-if="trError" class="rounded-md bg-bad-soft px-2 py-1.5 text-2xs text-bad">{{ trError }}</p>
      </template>
    </div>

    <!-- 结果区固定在下半部分：写成什么样和正文对照着看才有意义 -->
    <div class="flex max-h-[52%] shrink-0 flex-col border-t border-hairline">
      <div class="flex h-8 shrink-0 items-center gap-1.5 px-2.5">
        <span class="text-2xs font-semibold text-ink-2">{{ tab === 'draft' ? '草稿' : '译文' }}</span>
        <template v-if="tab === 'draft' && draft">
          <span :class="pillCls(groundingTone(draft.grounding_ratio))">
            有据率 {{ (draft.grounding_ratio * 100).toFixed(0) }}%
          </span>
          <span :class="pillCls('default')">{{ draft.retrieved_count }} 块证据</span>
        </template>
        <span v-if="tab === 'translate' && trResult" :class="pillCls('default')">
          {{ trResult.usage?.total_tokens ?? 0 }} tok
        </span>
        <span class="min-w-0 flex-1" />
        <button
          v-if="tab === 'draft' && draft"
          type="button"
          :class="btnCls('ghost', { size: 'sm' })"
          @click="downloadDraft"
        >
          <PhDownloadSimple :size="11" />
          .md
        </button>
      </div>

      <div class="min-h-0 flex-1 overflow-y-auto scroll-slim px-2.5 pb-2.5">
        <template v-if="tab === 'draft'">
          <div v-if="drafting" class="space-y-2">
            <span v-for="i in 5" :key="i" class="block h-3.5 animate-pulse rounded-sm bg-sunken" />
          </div>
          <div v-else-if="!draft" :class="EMPTY_CLS">
            <strong class="text-[12.5px] text-ink-2">还没有草稿</strong>
            <span class="text-2xs leading-relaxed text-ink-4">
              填主题后点「生成草稿」。开启「先检索证据」时，正文里的 [n] 可点击核对原文。
            </span>
          </div>
          <template v-else>
            <MarkdownView :source="draft.content" :verified="verifiedMarkers" />
            <div v-if="draftCitations.length" class="mt-3">
              <p class="mb-1 text-2xs font-medium text-ink-3">引用</p>
              <CitationList :citations="draftCitations" />
            </div>
          </template>
        </template>

        <template v-else>
          <div v-if="translating" class="space-y-2">
            <span v-for="i in 5" :key="i" class="block h-3.5 animate-pulse rounded-sm bg-sunken" />
          </div>
          <div v-else-if="!trResult" :class="EMPTY_CLS">
            <strong class="text-[12.5px] text-ink-2">还没有译文</strong>
            <span class="text-2xs text-ink-4">粘贴原文后点「翻译」。</span>
          </div>
          <template v-else>
            <template v-if="trResult.pairs?.length">
              <div v-for="(p, i) in trResult.pairs" :key="i" class="border-b border-dashed border-hairline py-1.5">
                <p class="text-2xs leading-relaxed text-ink-4">{{ p.source }}</p>
                <p class="mt-0.5 text-[12.5px] leading-relaxed text-ink">{{ p.target }}</p>
              </div>
            </template>
            <MarkdownView v-else :source="trResult.text" />
          </template>
        </template>
      </div>
    </div>
  </div>
</template>
