<script setup lang="ts">
/**
 * 拖拽上传 PDF。原生 input[type=file] + dragover/drop，不引上传组件。
 *
 * 上传后由 store 起轮询（`startPolling`），状态在表格里就地变成「可检索」，
 * 这里只负责把"已经进入后台"这件事说清楚。
 */
import { PhFilePdf, PhUploadSimple } from '@phosphor-icons/vue'
import type { PaperUploadResult } from '~/types/api'

const emit = defineEmits<{ uploaded: [result: PaperUploadResult] }>()
const papers = usePapersStore()

const MAX_MB = 100

const title = ref('')
const busy = ref(false)
const dragging = ref(false)
const errorMessage = ref('')
const lastResult = ref<PaperUploadResult | null>(null)
const picker = ref<HTMLInputElement | null>(null)

function accept(file: File): string | null {
  const isPdf = file.name.toLowerCase().endsWith('.pdf') || file.type === 'application/pdf'
  if (!isPdf) return '只接受 PDF 文件'
  if (file.size > MAX_MB * 1024 * 1024) {
    return `文件超过 ${MAX_MB}MB 上限（当前 ${(file.size / 1024 / 1024).toFixed(1)}MB）`
  }
  return null
}

async function upload(file: File) {
  const bad = accept(file)
  if (bad) {
    errorMessage.value = bad
    return
  }
  busy.value = true
  errorMessage.value = ''
  try {
    const res = await papers.upload(file, title.value)
    lastResult.value = res
    title.value = ''
    emit('uploaded', res)
  } catch (err) {
    errorMessage.value = (err as Error).message
  } finally {
    busy.value = false
  }
}

async function onDrop(e: DragEvent) {
  dragging.value = false
  const files = Array.from(e.dataTransfer?.files ?? [])
  // 串行：并发上传会同时打满嵌入模型，反而更慢
  for (const f of files) await upload(f)
}

function onPick(e: Event) {
  const input = e.target as HTMLInputElement
  const f = input.files?.[0]
  if (f) void upload(f)
  input.value = '' // 允许同一个文件再选一次
}
</script>

<template>
  <div class="uploader rc-panel">
    <div
      class="drop"
      :class="{ dragging, busy }"
      role="button"
      tabindex="0"
      aria-label="上传 PDF"
      @click="picker?.click()"
      @keydown.enter.prevent="picker?.click()"
      @keydown.space.prevent="picker?.click()"
      @dragover.prevent="dragging = true"
      @dragleave.prevent="dragging = false"
      @drop.prevent="onDrop"
    >
      <input ref="picker" class="hidden-input" type="file" accept="application/pdf,.pdf" @change="onPick" />

      <PhUploadSimple v-if="!busy" :size="20" class="drop-icon" />
      <span v-else class="rc-spin" />

      <p class="drop-main">
        {{ busy ? '上传中…' : dragging ? '松手即上传' : '把论文拖到这里' }}
      </p>
      <p class="rc-caption">或点击选择 · 仅 PDF · 单个 ≤ {{ MAX_MB }}MB</p>
    </div>

    <label class="rc-field" style="margin-top: 10px">
      <span class="rc-label">标题</span>
      <input v-model="title" class="rc-input" placeholder="留空则用文件名" :disabled="busy" />
    </label>

    <p class="rc-caption" style="margin: 9px 0 0; line-height: 1.6">
      <PhFilePdf :size="12" style="vertical-align: -1px" />
      解析链：MinerU（版面 / 公式 / 表格）→ 失败降级 PyMuPDF → pdfplumber → pypdf → 按章节切块 → bge-m3 双向量 → Milvus。
      实际生效的解析器会写在列表里。<strong>状态变成「可检索」</strong>才会进入问答候选集。
    </p>

    <p v-if="errorMessage" class="rc-alert rc-alert--bad" style="margin-top: 9px">{{ errorMessage }}</p>

    <p v-if="lastResult" class="rc-alert rc-alert--info" style="margin-top: 9px; display: block">
      已提交后台解析：<strong>{{ lastResult.paper.title || lastResult.paper.id.slice(0, 8) }}</strong>
      <br />
      <span class="rc-caption">列表状态会自动刷新，无需手动重试。</span>
    </p>
  </div>
</template>

<style scoped>
.uploader {
  padding: 12px;
}

.hidden-input {
  display: none;
}

.drop {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
  padding: 20px 10px;
  border: 1px dashed var(--rc-hairline-strong);
  border-radius: var(--rc-radius-md);
  background: var(--rc-canvas);
  color: var(--rc-ink-subtle);
  cursor: pointer;
  transition:
    border-color 0.14s ease,
    background 0.14s ease;
}
.drop:hover {
  border-color: var(--rc-ink-tertiary);
}
.drop.dragging {
  border-color: var(--rc-primary);
  background: var(--rc-primary-soft);
}
.drop.busy {
  cursor: progress;
}
.drop-icon {
  color: var(--rc-primary-hover);
}

.drop-main {
  margin: 2px 0 0;
  font-size: 13px;
  font-weight: 500;
  color: var(--rc-ink-muted);
}
</style>
