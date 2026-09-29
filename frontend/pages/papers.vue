<script setup lang="ts">
/** 文献库：左上传 + 右表格；点行打开阅读器。 */
import type { Paper } from '~/types/api'

const papers = usePapersStore()

const reader = reactive({
  open: false,
  paper: null as Paper | null,
})

function openReader(p: Paper) {
  reader.paper = p
  reader.open = true
}

const pdfUrl = computed(() => (reader.paper ? papers.pdfUrl(reader.paper.id) : ''))

onMounted(() => void papers.load())
</script>

<template>
  <div class="rc-page rc-page--scroll">
    <header class="rc-page-head">
      <h1>文献库</h1>
      <p>
        PDF 上传后走：MinerU 版面还原（OCR / 公式 / 表格）→ 按章节切块 → bge-m3 双向量 → 写入 Milvus。
        MinerU 不可用时自动降级 PyMuPDF → pdfplumber → pypdf，页面上会标出实际生效的解析器。
        <b>状态变成「可检索」</b>才会进入问答的候选集。
      </p>
    </header>

    <div class="grid">
      <PaperUploader />

      <div class="table-wrap rc-panel">
        <PaperTable @open="openReader" />
      </div>
    </div>

    <Drawer :open="reader.open" :title="reader.paper?.title || '阅读'" @close="reader.open = false">
      <div class="drawer-body">
        <ClientOnly>
          <PdfViewer v-if="reader.paper" :url="pdfUrl" />
          <template #fallback><div class="rc-empty">正在加载阅读器…</div></template>
        </ClientOnly>
      </div>
    </Drawer>
  </div>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 12px;
  align-items: start;
}

.table-wrap {
  padding: 12px;
}

.drawer-body {
  height: 100%;
}
</style>
