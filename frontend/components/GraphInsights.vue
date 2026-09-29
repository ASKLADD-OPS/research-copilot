<script setup lang="ts">
/**
 * 图谱分析面板：overview / pagerank / communities / paths。
 *
 * 四个分析是互斥视图，用分段控件而不是标签页 —— 标签页会带一堆自己用不到的
 * 键盘/动画/懒加载契约，这里只想要"按一下换一组数"。
 */
import type { AnalysisKind, GraphAnalysisResult, GraphNode, Paper } from '~/types/api'

const props = defineProps<{
  selected?: GraphNode | null
  papers?: Paper[]
}>()

const api = useApi()
const analysis = ref<AnalysisKind>('overview')
const result = ref<GraphAnalysisResult | null>(null)
const loading = ref(false)
const errorMessage = ref('')

const pathFrom = ref<string>('')
const pathTo = ref<string>('')

// 只暴露后端真正实现了分支的四种；centrality/timeline 目前会落到 overview，
// 放出来只会让用户以为有功能其实没有。
const KINDS: { value: AnalysisKind; label: string }[] = [
  { value: 'overview', label: '总览' },
  { value: 'pagerank', label: 'PageRank' },
  { value: 'communities', label: '社群' },
  { value: 'paths', label: '最短路径' },
]

async function run() {
  loading.value = true
  errorMessage.value = ''
  try {
    result.value = await api.post<GraphAnalysisResult>('/graph/analyze', {
      analysis: analysis.value,
      source: pathFrom.value || null,
      target: pathTo.value || null,
    })
  } catch (err) {
    errorMessage.value = (err as Error).message
    result.value = null
  } finally {
    loading.value = false
  }
}

watch(analysis, () => void run())

const titleOf = (id: string) => props.papers?.find((p) => p.id === id)?.title || id.slice(0, 8)

const overview = computed(() => {
  if (analysis.value !== 'overview' || !result.value?.result) return null
  return result.value.result as {
    n_nodes: number
    n_edges: number
    density: number
    avg_degree: number
    top_cited: { paper_id: string; title: string; in_degree: number }[]
  }
})

// 后端的 result 字段是"按 analysis 变形的"，声明成 unknown。
// 收窄放在 script 里做 —— vue-tsc 解析不了模板里的 `x as T`（会报 TS1005）。
interface PagerankRow { paper_id: string; title: string; score: number }
interface CommunityRow { size: number; papers: { paper_id: string; title: string }[] }
interface PathPayload { path?: string[]; length?: number; error?: string }

const listResult = computed<unknown[]>(() =>
  Array.isArray(result.value?.result) ? result.value!.result : [],
)
const pagerankRows = computed(() => listResult.value as PagerankRow[])
const communityRows = computed(() => listResult.value as CommunityRow[])
const pathPayload = computed<PathPayload | null>(() => {
  const r = result.value?.result
  return analysis.value === 'paths' && r && !Array.isArray(r) ? (r as PathPayload) : null
})
</script>

<template>
  <div class="insights rc-panel">
    <div class="head">
      <Tabs :model-value="analysis" :options="KINDS" @update:model-value="analysis = $event as AnalysisKind" />
      <span class="rc-spacer" />
      <button class="rc-btn rc-btn--secondary rc-btn--sm" type="button" :disabled="loading" @click="run">
        <span v-if="loading" class="rc-spin" />
        重算
      </button>
    </div>

    <div v-if="analysis === 'paths'" class="paths">
      <select v-model="pathFrom" class="rc-select" aria-label="起点论文">
        <option value="">起点论文</option>
        <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title || p.id.slice(0, 8) }}</option>
      </select>
      <select v-model="pathTo" class="rc-select" aria-label="终点论文">
        <option value="">终点论文</option>
        <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title || p.id.slice(0, 8) }}</option>
      </select>
      <button
        class="rc-btn rc-btn--primary rc-btn--sm"
        type="button"
        :disabled="!pathFrom || !pathTo || loading"
        @click="run"
      >
        找路径
      </button>
    </div>

    <p v-if="errorMessage" class="rc-alert rc-alert--bad notice">{{ errorMessage }}</p>

    <div class="body rc-scroll">
      <!-- 选中的节点优先展示：点击图上的点就该立刻看到它的指标 -->
      <div v-if="selected" class="block">
        <div class="block-title">选中节点</div>
        <div class="kv"><span>标题</span><b>{{ selected.title || selected.id }}</b></div>
        <div class="kv"><span>被引 / 引用</span><b>{{ selected.in_degree }} / {{ selected.out_degree }}</b></div>
        <div class="kv"><span>年份</span><b>{{ selected.year ?? '—' }}</b></div>
        <div class="kv"><span>社群</span><b>{{ selected.community ?? '—' }}</b></div>
        <div class="kv"><span>PageRank</span><b>{{ selected.pagerank?.toFixed(5) ?? '—' }}</b></div>
      </div>

      <div v-if="overview" class="block">
        <div class="block-title">总览</div>
        <div class="kv"><span>节点 / 边</span><b>{{ overview.n_nodes }} / {{ overview.n_edges }}</b></div>
        <div class="kv"><span>密度</span><b>{{ overview.density }}</b></div>
        <div class="kv"><span>平均度</span><b>{{ overview.avg_degree }}</b></div>
        <div v-if="overview.top_cited?.length" class="list">
          <div class="block-title" style="margin-top: 8px">被引最多</div>
          <div v-for="t in overview.top_cited" :key="t.paper_id" class="list-item">
            <span class="rc-grow rc-truncate" :title="t.title">{{ t.title || titleOf(t.paper_id) }}</span>
            <span class="rc-pill rc-pill--dim">{{ t.in_degree }}</span>
          </div>
        </div>
      </div>

      <div v-else-if="analysis === 'pagerank' && pagerankRows.length" class="block">
        <div class="block-title">PageRank 前 20</div>
        <div v-for="(r, i) in pagerankRows" :key="r.paper_id" class="list-item">
          <span class="rc-mono rc-muted" style="width: 22px">{{ i + 1 }}</span>
          <span class="rc-grow rc-truncate" :title="r.title">{{ r.title }}</span>
          <span class="rc-mono">{{ r.score.toFixed(4) }}</span>
        </div>
      </div>

      <div v-else-if="analysis === 'communities' && communityRows.length" class="block">
        <div class="block-title">社群（Louvain）</div>
        <div v-for="(c, i) in communityRows" :key="i" class="comm">
          <div class="rc-row">
            <span class="rc-pill">社群 {{ i + 1 }}</span>
            <span class="rc-caption">{{ c.size }} 篇</span>
          </div>
          <div v-for="p in c.papers" :key="p.paper_id" class="list-item">
            <span class="rc-grow rc-truncate" :title="p.title">{{ p.title }}</span>
          </div>
        </div>
      </div>

      <div v-else-if="analysis === 'paths'" class="block">
        <div class="block-title">最短引用路径</div>
        <template v-if="pathPayload">
          <div v-if="pathPayload.error" class="rc-muted">{{ pathPayload.error }}</div>
          <template v-else>
            <div v-for="(pid, i) in pathPayload.path || []" :key="pid" class="path-node">
              <span class="rc-pill rc-pill--dim">{{ i + 1 }}</span>
              <span class="rc-grow rc-truncate">{{ titleOf(pid) }}</span>
            </div>
            <div class="rc-caption" style="margin-top: 6px">长度 {{ pathPayload.length ?? 0 }} 跳</div>
          </template>
        </template>
        <div v-else class="rc-muted">选择起点与终点后点「找路径」。</div>
      </div>

      <div v-if="loading" class="skeletons">
        <span v-for="i in 3" :key="i" class="rc-skeleton" style="height: 26px" />
      </div>
      <!-- 有选中节点时上面那块已经在给数了，不再叠一句"去看标签"的空态 -->
      <div v-else-if="!result && !selected" class="rc-empty">切换上方标签看分析结果</div>

      <div v-if="result?.note && !loading" class="rc-caption">{{ result.note }}</div>
    </div>
  </div>
</template>

<style scoped>
.insights {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--rc-hairline);
  background: var(--rc-surface-1);
}

.paths {
  display: grid;
  grid-template-columns: 1fr 1fr auto;
  gap: 6px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--rc-hairline);
}

.notice {
  margin: 8px 10px 0;
}

.body {
  flex: 1;
  padding: 10px;
}

.block {
  margin-bottom: 12px;
}
.block-title {
  font-family: var(--rc-font-display);
  font-weight: 600;
  font-size: 12.5px;
  margin-bottom: 5px;
  color: var(--rc-ink);
}

.kv {
  display: grid;
  grid-template-columns: 82px 1fr;
  gap: 6px;
  font-size: 12.5px;
  padding: 2px 0;
}
.kv span {
  color: var(--rc-ink-tertiary);
}

.list-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 0;
  font-size: 12.5px;
}

.comm {
  border-top: 1px dashed var(--rc-hairline);
  padding-top: 7px;
  margin-top: 7px;
}

.path-node {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  padding: 2px 0;
}

.skeletons {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin-bottom: 10px;
}
</style>
