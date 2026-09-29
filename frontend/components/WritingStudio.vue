<script setup lang="ts">
/**
 * 写作工作室：章节草稿 + 翻译。
 *
 * 与聊天页的区别是"有据生成"被显式化：写作前先按主题检索证据，
 * 生成后再过一遍溯源引擎，所以结果面板一定同时给出 grounding 与引用列表。
 */
import { PhDownloadSimple } from '@phosphor-icons/vue'
import type { Citation, TranslateResult, WriteResult, WritingTemplate } from '~/types/api'

const api = useApi()
const ui = useUiStore()

const MODES = [
  { value: 'draft', label: '章节草稿' },
  { value: 'translate', label: '翻译' },
] as const

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
      paper_ids: ui.scopePaperIds,
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
  <div class="studio">
    <!-- 左：输入 -->
    <div class="col rc-panel">
      <div class="rc-panel-head">
        <Tabs :model-value="tab" :options="MODES" @update:model-value="tab = $event as 'draft' | 'translate'" />
      </div>

      <div class="form rc-scroll">
        <template v-if="tab === 'draft'">
          <label class="rc-field">
            <span class="rc-label">模板</span>
            <select v-model="form.kind" class="rc-select">
              <option v-for="t in templates" :key="t.kind" :value="t.kind">{{ t.name }}</option>
            </select>
          </label>

          <div v-if="currentTemplate" class="tpl-hint">
            <div class="rc-caption">{{ currentTemplate.description }}</div>
            <div class="outline">
              <span v-for="(o, i) in currentTemplate.outline" :key="o" class="rc-pill rc-pill--dim">
                {{ i + 1 }}. {{ o }}
              </span>
            </div>
          </div>

          <label class="rc-field">
            <span class="rc-label">写什么</span>
            <textarea
              v-model="form.topic"
              class="rc-textarea"
              rows="4"
              placeholder="例如：对比这三篇论文在检索增强方法上的差异，指出各自的适用边界"
            />
          </label>

          <div class="two-up">
            <label class="rc-field">
              <span class="rc-label">语言</span>
              <select v-model="form.language" class="rc-select">
                <option value="zh">中文</option>
                <option value="en">English</option>
              </select>
            </label>
            <label class="rc-field">
              <span class="rc-label">目标字数</span>
              <input
                v-model.number="form.target_length"
                class="rc-input rc-mono"
                type="number"
                min="100"
                max="8000"
                step="100"
              />
            </label>
          </div>

          <label class="rc-field">
            <span class="rc-label">风格要求</span>
            <input v-model="form.style" class="rc-input" placeholder="如：学术、克制、避免口语与排比" />
          </label>

          <label class="rc-switch">
            <input v-model="form.use_retrieval" type="checkbox" />
            先检索证据再写（关闭则纯生成，不再有引用与有据率）
          </label>

          <p class="rc-caption" style="margin: 0">
            证据范围：{{ ui.scopePaperIds.length ? `限定 ${ui.scopePaperIds.length} 篇` : '全库' }}
            · 可在对话页调整
          </p>

          <button
            class="rc-btn rc-btn--primary rc-btn--block"
            type="button"
            :disabled="drafting || !form.topic.trim()"
            @click="generate"
          >
            <span v-if="drafting" class="rc-spin" />
            生成草稿
          </button>

          <p v-if="draftError" class="rc-alert rc-alert--bad">{{ draftError }}</p>
        </template>

        <template v-else>
          <label class="rc-field">
            <span class="rc-label">原文</span>
            <textarea v-model="tr.text" class="rc-textarea" rows="10" placeholder="粘贴要翻译的段落，可多段（空行分段）" />
          </label>

          <label class="rc-field">
            <span class="rc-label">目标语言</span>
            <select v-model="tr.target" class="rc-select">
              <option value="zh">中文</option>
              <option value="en">English</option>
            </select>
          </label>

          <label class="rc-switch">
            <input v-model="tr.keep_terms" type="checkbox" />
            保留术语原文（括号内给译名）
          </label>
          <label class="rc-switch">
            <input v-model="tr.bilingual" type="checkbox" />
            逐段对照输出
          </label>

          <button
            class="rc-btn rc-btn--primary rc-btn--block"
            type="button"
            :disabled="translating || !tr.text.trim()"
            @click="translate"
          >
            <span v-if="translating" class="rc-spin" />
            翻译
          </button>

          <p v-if="trError" class="rc-alert rc-alert--bad">{{ trError }}</p>
        </template>
      </div>
    </div>

    <!-- 右：结果 -->
    <div class="col rc-panel">
      <div class="rc-panel-head">
        <b class="rc-panel-title rc-grow">{{ tab === 'draft' ? '草稿' : '译文' }}</b>
        <template v-if="tab === 'draft' && draft">
          <span class="rc-pill" :class="draft.grounding_ratio >= 0.8 ? 'rc-pill--ok' : 'rc-pill--warn'">
            有据率 {{ (draft.grounding_ratio * 100).toFixed(0) }}%
          </span>
          <span class="rc-pill rc-pill--dim">{{ draft.retrieved_count }} 块证据</span>
          <button class="rc-btn rc-btn--ghost rc-btn--sm" type="button" @click="downloadDraft">
            <PhDownloadSimple :size="13" />
            下载 .md
          </button>
        </template>
        <span v-if="tab === 'translate' && trResult" class="rc-pill rc-pill--dim">
          {{ trResult.usage?.calls ?? 0 }} 次调用 · {{ trResult.usage?.total_tokens ?? 0 }} tok
        </span>
      </div>

      <div class="result-body rc-scroll">
        <template v-if="tab === 'draft'">
          <div v-if="drafting" class="skeletons">
            <span v-for="i in 5" :key="i" class="rc-skeleton" style="height: 15px" />
          </div>

          <div v-else-if="!draft" class="rc-empty">
            <strong>还没有草稿</strong>
            <span>填写主题后点「生成草稿」。开启「先检索证据」时，正文里的 [n] 可点击核对原文。</span>
          </div>

          <template v-else>
            <MarkdownView :source="draft.content" :verified="verifiedMarkers" />

            <div v-if="draftCitations.length" style="margin-top: 14px">
              <div class="block-title">引用</div>
              <CitationCard v-for="c in draftCitations" :key="`${c.marker}-${c.chunk_id}`" :citation="c" />
            </div>
          </template>
        </template>

        <template v-else>
          <div v-if="translating" class="skeletons">
            <span v-for="i in 5" :key="i" class="rc-skeleton" style="height: 15px" />
          </div>

          <div v-else-if="!trResult" class="rc-empty">
            <strong>还没有译文</strong>
            <span>粘贴原文后点「翻译」。</span>
          </div>

          <template v-else>
            <template v-if="trResult.pairs?.length">
              <div v-for="(p, i) in trResult.pairs" :key="i" class="pair">
                <div class="pair-src">{{ p.source }}</div>
                <div class="pair-dst">{{ p.target }}</div>
              </div>
            </template>
            <MarkdownView v-else :source="trResult.text" />
          </template>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.studio {
  display: grid;
  grid-template-columns: 400px 1fr;
  gap: 12px;
  height: calc(100vh - 96px);
}

.col {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.form {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  overflow-y: auto;
}

.two-up {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.tpl-hint {
  padding: 8px 10px;
  background: var(--rc-surface-2);
  border-radius: var(--rc-radius-md);
}
.outline {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}

.result-body {
  flex: 1;
  padding: 14px;
}

.block-title {
  margin-bottom: 6px;
  font-family: var(--rc-font-display);
  font-weight: 600;
  font-size: 12.5px;
  color: var(--rc-ink);
}

.skeletons {
  display: flex;
  flex-direction: column;
  gap: 9px;
}

.pair {
  border-bottom: 1px dashed var(--rc-hairline);
  padding: 8px 0;
}
.pair-src {
  font-size: 12.5px;
  color: var(--rc-ink-subtle);
}
.pair-dst {
  margin-top: 4px;
  font-size: 13.5px;
}
</style>
