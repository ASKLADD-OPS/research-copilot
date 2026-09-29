<script setup lang="ts">
/** 引文图谱页：左图右分析。SSR 关闭（见 nuxt.config routeRules）。 */
import { PhArrowsClockwise } from '@phosphor-icons/vue'
import type { GraphNode, GraphOut } from '~/types/api'

const api = useApi()
const papers = usePapersStore()

const graph = ref<GraphOut | null>(null)
const loading = ref(false)
const errorMessage = ref('')
const selected = ref<GraphNode | null>(null)

async function load() {
  loading.value = true
  errorMessage.value = ''
  try {
    graph.value = await api.get<GraphOut>('/graph')
    selected.value = null
  } catch (err) {
    errorMessage.value = (err as Error).message
    graph.value = null
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await Promise.all([load(), papers.load()])
})
</script>

<template>
  <div class="rc-page">
    <header class="rc-page-head">
      <h1>引文图谱</h1>
      <p>
        边方向 = 引用方向（citing → cited）。引用边来自解析出的参考文献与元数据匹配，
        可在工具页调用 <code class="rc-mono">rebuild_citation_edges</code> 重建。
      </p>
    </header>

    <div class="toolbar rc-row">
      <button class="rc-btn rc-btn--secondary rc-btn--sm" type="button" :disabled="loading" @click="load">
        <PhArrowsClockwise :size="13" :class="{ spin: loading }" />
        重新加载
      </button>
      <span v-if="graph" class="rc-pill rc-pill--dim">{{ graph.n_nodes }} 节点 · {{ graph.n_edges }} 边</span>
      <span v-if="graph?.truncated" class="rc-pill rc-pill--warn">节点过多，仅显示核心子图（按度数裁剪）</span>
      <span class="rc-spacer" />
    </div>

    <p v-if="errorMessage" class="rc-alert rc-alert--bad notice">{{ errorMessage }}</p>

    <div class="grid">
      <ClientOnly>
        <CitationGraph
          v-if="graph"
          :nodes="graph.nodes"
          :edges="graph.edges"
          :loading="loading"
          @select="selected = $event"
        />
        <template #fallback><div class="rc-panel rc-empty">正在初始化图形引擎…</div></template>
      </ClientOnly>

      <GraphInsights :selected="selected" :papers="papers.items" />
    </div>
  </div>
</template>

<style scoped>
.toolbar {
  margin-bottom: 10px;
}

.notice {
  margin: 0 0 10px;
}

.grid {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 340px;
  gap: 12px;
  min-height: 0;
  overflow: hidden;
}

.spin {
  animation: rc-rotate 0.7s linear infinite;
}
</style>
