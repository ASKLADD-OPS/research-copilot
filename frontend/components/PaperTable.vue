<script setup lang="ts">
/**
 * 文献库表格：状态、搜索、重解析、删除、分块预览、元数据编辑。
 *
 * 用原生 <table> 而不是表格组件：列是固定的六列，没有虚拟滚动、没有列拖拽、
 * 没有行选择，引一个表格库只为这些等于白交一份运行时开销。
 * 表头 sticky、hover 高亮由 .rc-table 提供。
 */
import { PhArrowsClockwise, PhMagnifyingGlass, PhPencilSimple, PhTrash } from '@phosphor-icons/vue'
import type { Paper, PaperChunk, PaperStatus } from '~/types/api'

const emit = defineEmits<{ open: [paper: Paper] }>()
const papers = usePapersStore()

const STATUS: Record<PaperStatus, { text: string; cls: string }> = {
  pending: { text: '待解析', cls: 'rc-pill--dim' },
  parsing: { text: '解析中', cls: 'rc-pill--warn' },
  indexing: { text: '入库中', cls: 'rc-pill--warn' },
  ready: { text: '可检索', cls: 'rc-pill--ok' },
  failed: { text: '失败', cls: 'rc-pill--bad' },
}
const STATUS_OPTIONS = Object.entries(STATUS) as [PaperStatus, { text: string }][]

const notice = ref('')
let noticeTimer: ReturnType<typeof setTimeout> | null = null
function flash(text: string) {
  notice.value = text
  if (noticeTimer) clearTimeout(noticeTimer)
  noticeTimer = setTimeout(() => (notice.value = ''), 4000)
}

// ---------------- 搜索（防抖，不然每敲一个字打一次后端） ----------------
let searchTimer: ReturnType<typeof setTimeout> | null = null
watch(
  () => papers.query,
  () => {
    if (searchTimer) clearTimeout(searchTimer)
    searchTimer = setTimeout(() => {
      papers.page = 1
      void papers.load()
    }, 300)
  },
)
watch(
  () => papers.statusFilter,
  () => {
    papers.page = 1
    void papers.load()
  },
)

// ---------------- 分块预览 ----------------
const chunkOpen = ref(false)
const chunkLoading = ref(false)
const chunkPaper = ref<Paper | null>(null)
const chunks = ref<PaperChunk[]>([])

async function showChunks(row: Paper) {
  chunkPaper.value = row
  chunkOpen.value = true
  chunkLoading.value = true
  chunks.value = []
  try {
    chunks.value = await papers.fetchChunks(row.id, 200)
  } catch (err) {
    flash(`分块读取失败：${(err as Error).message}`)
  } finally {
    chunkLoading.value = false
  }
}

// ---------------- 编辑 ----------------
const editOpen = ref(false)
const saving = ref(false)
const form = reactive({ id: '', title: '', year: '' as string, venue: '', tags: '' })

function openEdit(row: Paper) {
  Object.assign(form, {
    id: row.id,
    title: row.title,
    year: row.year == null ? '' : String(row.year),
    venue: row.venue ?? '',
    tags: (row.tags ?? []).join(', '),
  })
  editOpen.value = true
}

const yearError = computed(() => {
  if (!form.year.trim()) return ''
  const n = Number(form.year)
  return Number.isInteger(n) && n >= 1500 && n <= 2200 ? '' : '年份需在 1500-2200 之间'
})

async function saveEdit() {
  if (yearError.value) return
  saving.value = true
  try {
    await papers.update(form.id, {
      title: form.title,
      year: form.year.trim() ? Number(form.year) : null,
      venue: form.venue || null,
      tags: form.tags
        ? form.tags
            .split(',')
            .map((t) => t.trim())
            .filter(Boolean)
        : [],
    })
    editOpen.value = false
    flash('元数据已保存')
  } catch (err) {
    flash(`保存失败：${(err as Error).message}`)
  } finally {
    saving.value = false
  }
}

// ---------------- 删除 / 重解析 ----------------
const pendingDelete = ref<Paper | null>(null)
const deleting = ref(false)

async function doDelete() {
  const row = pendingDelete.value
  if (!row) return
  deleting.value = true
  try {
    await papers.remove(row.id)
    pendingDelete.value = null
    flash('已删除（向量、分块与本地 PDF 一并清除）')
  } catch (err) {
    flash(`删除失败：${(err as Error).message}`)
  } finally {
    deleting.value = false
  }
}

async function doReindex(row: Paper) {
  try {
    await papers.reindex(row.id)
    flash('已重新提交解析，状态会自动刷新')
  } catch (err) {
    flash(`重解析失败：${(err as Error).message}`)
  }
}

const totalPages = computed(() => Math.max(1, Math.ceil(papers.total / papers.pageSize)))

function goto(delta: number) {
  const next = papers.page + delta
  if (next < 1 || next > totalPages.value) return
  papers.page = next
  void papers.load()
}

function resetAndLoad() {
  papers.page = 1
  void papers.load()
}

onMounted(() => void papers.load())
onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
  if (noticeTimer) clearTimeout(noticeTimer)
  papers.stopAllPolling()
})
</script>

<template>
  <div class="wrap">
    <div class="toolbar">
      <label class="search">
        <PhMagnifyingGlass :size="13" class="search-icon" />
        <input v-model="papers.query" class="rc-input search-input" placeholder="按标题 / 摘要搜索" aria-label="搜索论文" />
      </label>

      <select v-model="papers.statusFilter" class="rc-select filter" aria-label="状态筛选">
        <option value="">全部状态</option>
        <option v-for="[value, meta] in STATUS_OPTIONS" :key="value" :value="value">{{ meta.text }}</option>
      </select>

      <button class="rc-btn rc-btn--secondary" type="button" :disabled="papers.loading" @click="papers.load()">
        <span v-if="papers.loading" class="rc-spin" />
        刷新
      </button>

      <span class="rc-spacer" />
      <span class="rc-caption">共 {{ papers.total }} 篇 · 本页可检索 {{ papers.readyCount }} 篇</span>
    </div>

    <p v-if="notice" class="rc-alert rc-alert--info notice">{{ notice }}</p>
    <p v-if="papers.errorMessage" class="rc-alert rc-alert--bad notice">{{ papers.errorMessage }}</p>

    <div class="table-wrap rc-scroll">
      <table class="rc-table">
        <thead>
          <tr>
            <th>标题</th>
            <th style="width: 96px">状态</th>
            <th style="width: 66px; text-align: right">分块</th>
            <th style="width: 56px; text-align: right">页</th>
            <th style="width: 76px; text-align: right">大小</th>
            <th style="width: 232px" />
          </tr>
        </thead>

        <tbody>
          <tr v-for="row in papers.items" :key="row.id">
            <td>
              <button class="title" type="button" :disabled="row.status !== 'ready'" @click="emit('open', row)">
                {{ row.title || '（未命名）' }}
              </button>
              <div class="rc-mono meta">
                {{ [row.venue, row.year].filter(Boolean).join(' · ') || row.source }}
                <template v-if="row.arxiv_id"> · arXiv:{{ row.arxiv_id }}</template>
                <template v-if="row.parser"> · {{ row.parser }}</template>
              </div>
              <p v-if="row.error" class="err rc-truncate" :title="row.error">{{ row.error }}</p>
            </td>

            <td>
              <span class="rc-pill" :class="STATUS[row.status].cls" :title="row.error || STATUS[row.status].text">
                <span v-if="row.status === 'parsing' || row.status === 'indexing'" class="rc-dot rc-dot--live" />
                {{ STATUS[row.status].text }}
              </span>
            </td>

            <td style="text-align: right" class="rc-mono">{{ row.num_chunks || '—' }}</td>
            <td style="text-align: right" class="rc-mono">{{ row.page_count ?? '—' }}</td>
            <td style="text-align: right" class="rc-mono">{{ formatBytes(row.file_size) }}</td>

            <td>
              <div class="actions">
                <button
                  class="rc-btn rc-btn--bare rc-btn--sm"
                  type="button"
                  :disabled="row.status !== 'ready'"
                  @click="emit('open', row)"
                >
                  阅读
                </button>
                <button class="rc-btn rc-btn--bare rc-btn--sm" type="button" @click="showChunks(row)">分块</button>
                <button class="rc-btn rc-btn--bare rc-btn--sm" type="button" title="编辑元数据" @click="openEdit(row)">
                  <PhPencilSimple :size="12" />
                </button>
                <button class="rc-btn rc-btn--bare rc-btn--sm" type="button" title="重新解析" @click="doReindex(row)">
                  <PhArrowsClockwise :size="12" />
                </button>
                <button class="rc-btn rc-btn--bare rc-btn--sm danger" type="button" title="删除" @click="pendingDelete = row">
                  <PhTrash :size="12" />
                </button>
              </div>
            </td>
          </tr>

          <tr v-if="papers.loading && !papers.items.length">
            <td colspan="6">
              <div class="skeleton-rows">
                <span v-for="i in 5" :key="i" class="rc-skeleton" style="height: 30px" />
              </div>
            </td>
          </tr>

          <tr v-else-if="!papers.items.length">
            <td colspan="6">
              <div class="rc-empty">
                <strong>文献库是空的</strong>
                <span>{{ papers.query || papers.statusFilter ? '换个搜索条件试试' : '左侧上传一份 PDF，解析完就能问答' }}</span>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="pager">
      <select v-model.number="papers.pageSize" class="rc-select page-size" aria-label="每页条数" @change="resetAndLoad">
        <option :value="10">10 条/页</option>
        <option :value="20">20 条/页</option>
        <option :value="50">50 条/页</option>
      </select>
      <span class="rc-spacer" />
      <span class="rc-caption rc-mono">{{ papers.page }} / {{ totalPages }}</span>
      <button class="rc-btn rc-btn--secondary rc-btn--sm" type="button" :disabled="papers.page <= 1" @click="goto(-1)">
        上一页
      </button>
      <button
        class="rc-btn rc-btn--secondary rc-btn--sm"
        type="button"
        :disabled="papers.page >= totalPages"
        @click="goto(1)"
      >
        下一页
      </button>
    </div>

    <!-- 分块预览：核对 chunker 到底切出了什么 -->
    <Modal :open="chunkOpen" :title="`分块预览 · ${chunkPaper?.title || ''}`" @close="chunkOpen = false">
      <div v-if="chunkLoading" class="skeleton-rows">
        <span v-for="i in 4" :key="i" class="rc-skeleton" style="height: 58px" />
      </div>

      <div v-else-if="chunks.length" class="chunk-list">
        <article v-for="c in chunks" :key="c.id" class="chunk rc-panel rc-panel--flush">
          <div class="rc-row chunk-head">
            <span class="rc-pill rc-pill--dim">#{{ c.chunk_index }}</span>
            <span v-if="c.section_name" class="rc-pill">{{ c.section_name }}</span>
            <span v-if="c.page_start" class="rc-pill rc-pill--dim">
              p.{{ c.page_start }}<template v-if="c.page_end && c.page_end !== c.page_start">-{{ c.page_end }}</template>
            </span>
            <span class="rc-spacer" />
            <span class="rc-mono rc-muted">{{ c.token_count }} tok · {{ c.content.length }} 字</span>
          </div>
          <p class="chunk-text">{{ c.content }}</p>
        </article>
      </div>

      <div v-else class="rc-empty">
        <strong>没有分块</strong>
        <span>论文可能还没解析完，或解析失败（看列表里的错误信息）。</span>
      </div>
    </Modal>

    <!-- 编辑元数据 -->
    <Modal :open="editOpen" title="编辑元数据" @close="editOpen = false">
      <div class="form">
        <label class="rc-field">
          <span class="rc-label">标题</span>
          <input v-model="form.title" class="rc-input" />
        </label>

        <label class="rc-field">
          <span class="rc-label">年份</span>
          <input
            v-model="form.year"
            class="rc-input"
            inputmode="numeric"
            placeholder="如 2024"
            :aria-invalid="!!yearError"
          />
          <span v-if="yearError" class="rc-error">{{ yearError }}</span>
        </label>

        <label class="rc-field">
          <span class="rc-label">发表处</span>
          <input v-model="form.venue" class="rc-input" placeholder="期刊 / 会议" />
        </label>

        <label class="rc-field">
          <span class="rc-label">标签</span>
          <input v-model="form.tags" class="rc-input" placeholder="逗号分隔，如：RAG, 综述" />
          <span class="rc-hint">影响检索分组与图谱聚类的人工标注。</span>
        </label>
      </div>

      <template #footer>
        <button class="rc-btn rc-btn--secondary" type="button" @click="editOpen = false">取消</button>
        <button class="rc-btn rc-btn--primary" type="button" :disabled="saving || !!yearError" @click="saveEdit">
          <span v-if="saving" class="rc-spin" />
          保存
        </button>
      </template>
    </Modal>

    <!-- 删除确认 -->
    <Modal :open="!!pendingDelete" title="确认删除" @close="pendingDelete = null">
      <p style="margin: 0 0 6px">删除《{{ pendingDelete?.title || pendingDelete?.id.slice(0, 8) }}》？</p>
      <p class="rc-caption" style="margin: 0; line-height: 1.7">
        该论文的 Milvus 向量、PostgreSQL 分块记录与本地 PDF 会一并清除，不可恢复。
        由它派生的引文边也会失去目标节点。
      </p>
      <template #footer>
        <button class="rc-btn rc-btn--secondary" type="button" @click="pendingDelete = null">取消</button>
        <button class="rc-btn rc-btn--danger" type="button" :disabled="deleting" @click="doDelete">
          <span v-if="deleting" class="rc-spin" />
          删除
        </button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
.wrap {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.search {
  position: relative;
  width: 240px;
}
.search-icon {
  position: absolute;
  left: 9px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--rc-ink-tertiary);
  pointer-events: none;
}
.search-input {
  padding-left: 27px;
}

.filter {
  width: 122px;
}
.page-size {
  width: 108px;
}

.notice {
  margin: 0 0 10px;
}

.table-wrap {
  flex: 1;
  min-height: 0;
  border: 1px solid var(--rc-hairline);
  border-radius: var(--rc-radius-md);
  background: var(--rc-surface-1);
}

.title {
  display: block;
  max-width: 100%;
  padding: 0;
  background: none;
  border: none;
  color: var(--rc-ink);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.title:disabled {
  cursor: default;
  color: var(--rc-ink-muted);
}
.title:not(:disabled):hover {
  text-decoration: underline;
}

.meta {
  color: var(--rc-ink-tertiary);
  margin-top: 2px;
}
.err {
  margin: 3px 0 0;
  font-size: 11.5px;
  color: var(--rc-danger);
  max-width: 420px;
}

.actions {
  display: flex;
  align-items: center;
  gap: 2px;
  justify-content: flex-end;
}
.actions .danger:hover {
  color: var(--rc-danger);
}

.skeleton-rows {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px;
}

.pager {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
}

.chunk-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.chunk {
  padding: 9px 11px;
}
.chunk-head {
  margin-bottom: 5px;
}
.chunk-text {
  margin: 0;
  font-size: 12.5px;
  color: var(--rc-ink-muted);
  line-height: 1.65;
  white-space: pre-wrap;
  max-height: 190px;
  overflow: auto;
}

.form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
</style>
