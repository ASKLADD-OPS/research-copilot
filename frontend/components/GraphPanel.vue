<script setup lang="ts">
/**
 * 引文图谱面板：力导向画布 + 参数侧栏 + 节点卡片 + 分析弹窗。
 *
 * 参数放在**面板内**的侧栏里而不是全局设置页：调斥力、调边长这类操作
 * 必须"边看边调"，切到别的页面改数字再回来，等于逼用户靠记忆调参。
 */
import {
  PhArrowsClockwise,
  PhGraph,
  PhSlidersHorizontal,
  PhCrosshair,
  PhChartLine,
  PhFileText,
  PhFunnel,
} from '@phosphor-icons/vue'
import type { AnalysisKind } from '~/types/api'
import type { GraphColorBy, GraphLayout } from '~/types/workbench'

const graph = useGraphStore()
const library = useLibraryStore()
const selection = useSelectionStore()
const ui = useUiStore()

const showConfig = ref(true)

const LAYOUTS: ReadonlyArray<{ value: GraphLayout; label: string }> = [
  { value: 'force', label: '力导向' },
  { value: 'circular', label: '环形' },
  { value: 'none', label: '固定' },
]

const COLOR_BY: ReadonlyArray<{ value: GraphColorBy; label: string }> = [
  { value: 'community', label: '社群' },
  { value: 'year', label: '年份' },
  { value: 'degree', label: '度数' },
]

const ANALYSES: ReadonlyArray<{ value: AnalysisKind; label: string }> = [
  { value: 'overview', label: '总览' },
  { value: 'pagerank', label: 'PageRank 关键文献' },
  { value: 'communities', label: '社群划分' },
  { value: 'centrality', label: '中心性' },
  { value: 'timeline', label: '时间线' },
  { value: 'paths', label: '两篇之间最短路径' },
]

const analysisOpen = ref(false)
const pickedAnalysis = ref<AnalysisKind>('overview')

async function runAnalysis(kind: AnalysisKind) {
  pickedAnalysis.value = kind
  analysisOpen.value = true
  // 分析跟着当前的检索范围走：用户缩了范围却分析全库，结论会和他的直觉对不上
  await graph.analyze(kind, library.scopePaperIds)
}

/** 分析结果可能是字符串、数组或嵌套对象，统一折成可读文本。 */
const analysisText = computed(() => {
  const r = graph.analysis?.result
  if (r == null) return ''
  if (typeof r === 'string') return r
  return JSON.stringify(r, null, 2)
})

function openNode(paperId: string) {
  void selection.openAt(paperId, 1)
  ui.setMainView('reader')
}

/** 把图谱里选中的节点收窄成检索范围。 */
function scopeToSelected(paperId: string) {
  library.scopePaperIds = [paperId]
}

onMounted(() => {
  graph.restoreConfig()
  if (!graph.loaded) void graph.load()
})
</script>

<template>
  <AppPanel
    title="引文图谱"
    :icon="PhGraph"
    side="left"
    :collapsible="false"
    :meta="graph.visibleNodes.length ? `${graph.visibleNodes.length} 节点 · ${graph.visibleEdges.length} 边` : ''"
  >
    <template #actions>
      <button type="button" :class="iconBtnCls('sm')" title="重新载入" @click="graph.load()">
        <PhArrowsClockwise :size="12" :class="{ 'animate-spin': graph.loading }" />
      </button>

      <button
        type="button"
        :class="[iconBtnCls('sm'), showConfig ? 'bg-hover text-ink' : '']"
        :title="showConfig ? '收起参数' : '展开参数'"
        @click="showConfig = !showConfig"
      >
        <PhSlidersHorizontal :size="12" />
      </button>

      <button
        type="button"
        :class="btnCls('ghost', { size: 'sm' })"
        :disabled="graph.loading"
        title="从库内文献的引用关系重建边"
        @click="graph.rebuild()"
      >
        重建引文边
      </button>

      <span class="mx-0.5 h-4 w-px bg-hairline" />

      <select
        :class="SELECT_CLS"
        class="!w-36"
        aria-label="图谱分析"
        @change="runAnalysis(($event.target as HTMLSelectElement).value as AnalysisKind)"
      >
        <option value="" disabled selected>图谱分析…</option>
        <option v-for="a in ANALYSES" :key="a.value" :value="a.value">{{ a.label }}</option>
      </select>
    </template>

    <div class="relative flex h-full">
      <div class="relative min-w-0 flex-1">
        <ClientOnly>
          <ForceGraph
            :nodes="graph.visibleNodes"
            :edges="graph.visibleEdges"
            :config="graph.config"
            :selected-id="graph.selectedNodeId"
            :max-in-degree="graph.maxInDegree"
            :max-page-rank="graph.maxPageRank"
            :max-degree="graph.maxDegree"
            @select="graph.selectNode($event)"
          />
          <template #fallback>
            <div class="grid h-full place-items-center"><AppSpinner :size="18" /></div>
          </template>
        </ClientOnly>

        <div
          v-if="graph.loading"
          class="absolute inset-0 grid place-items-center bg-surface/60"
        >
          <div class="flex items-center gap-2 text-[12px] text-ink-3">
            <AppSpinner :size="14" />
            正在布局…
          </div>
        </div>

        <div
          v-else-if="graph.isEmpty"
          class="absolute inset-0 grid place-items-center px-6"
        >
          <div :class="EMPTY_CLS">
            <PhGraph :size="22" class="mb-1.5 text-ink-4" />
            <strong class="text-[13px] text-ink-2">图上没有节点</strong>
            <span class="max-w-72 text-2xs leading-relaxed text-ink-4">
              {{ graph.errorMessage || '解释完文献后点「重建引文边」；已有数据但被参数滤空了就调低「最小度数」。' }}
            </span>
          </div>
        </div>

        <!-- 图例与操作提示：力导向图没有图例就不可能读懂 -->
        <div
          class="pointer-events-none absolute bottom-2.5 left-2.5 flex max-w-[70%] flex-wrap items-center gap-x-2.5 gap-y-1 rounded-md bg-surface/90 px-2 py-1.5 text-2xs text-ink-4 shadow-xs"
        >
          <span class="inline-flex items-center gap-1"><PhCrosshair :size="10" />滚轮缩放 · 拖拽平移</span>
          <span>点击节点选中</span>
          <span>箭头 = 引用方向（citing → cited）</span>
          <span v-if="graph.truncated" class="text-warn">已按度数裁剪到上限</span>
          <span v-if="graph.isolatedCount" class="text-warn">{{ graph.isolatedCount }} 个孤立节点</span>
        </div>

        <!-- 选中节点卡片 -->
        <div
          v-if="graph.selectedNode"
          class="absolute top-2.5 left-2.5 w-72 overflow-hidden rounded-lg border border-hairline bg-surface shadow-md"
        >
          <div class="flex items-start gap-2 border-b border-hairline px-2.5 py-2">
            <PhFileText :size="13" class="mt-0.5 shrink-0 text-brand" />
            <p class="flex-1 text-[12.5px] leading-snug font-medium text-ink">
              {{ graph.selectedNode.title || graph.selectedNode.id }}
            </p>
            <button
              type="button"
              class="shrink-0 text-2xs text-ink-4 hover:text-ink"
              @click="graph.selectNode(null)"
            >
              关闭
            </button>
          </div>
          <dl class="grid grid-cols-2 gap-x-3 gap-y-1 px-2.5 py-2 text-2xs">
            <div class="flex justify-between">
              <dt class="text-ink-4">被引</dt>
              <dd class="tabular-nums text-ink-2">{{ graph.selectedNode.in_degree }}</dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-ink-4">引用</dt>
              <dd class="tabular-nums text-ink-2">{{ graph.selectedNode.out_degree }}</dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-ink-4">年份</dt>
              <dd class="tabular-nums text-ink-2">{{ graph.selectedNode.year ?? '—' }}</dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-ink-4">社群</dt>
              <dd class="tabular-nums text-ink-2">{{ graph.selectedNode.community ?? '—' }}</dd>
            </div>
            <div class="col-span-2 flex justify-between">
              <dt class="text-ink-4">PageRank</dt>
              <dd class="tabular-nums text-ink-2">
                {{ graph.selectedNode.pagerank != null ? graph.selectedNode.pagerank.toFixed(5) : '—' }}
              </dd>
            </div>
          </dl>
          <div class="flex gap-1.5 border-t border-hairline px-2.5 py-2">
            <button
              type="button"
              :class="btnCls('primary', { size: 'sm' })"
              @click="openNode(graph.selectedNode.id)"
            >
              <PhFileText :size="11" />
              打开文献
            </button>
            <button
              type="button"
              :class="btnCls('ghost', { size: 'sm' })"
              title="把它设为检索范围，只在这篇的上下文里提问"
              @click="scopeToSelected(graph.selectedNode.id)"
            >
              <PhFunnel :size="11" />
              限定为此篇
            </button>
          </div>
        </div>
      </div>

      <!-- 参数侧栏 -->
      <aside
        v-if="showConfig"
        class="w-56 shrink-0 space-y-3 overflow-y-auto scroll-slim border-l border-hairline px-3 py-3"
      >
        <div class="flex items-center gap-2">
          <span class="text-2xs font-semibold text-ink-2">图谱参数</span>
          <span class="flex-1" />
          <button type="button" class="text-2xs text-ink-4 hover:text-ink" @click="graph.resetConfig()">
            重置
          </button>
        </div>

        <div :class="FIELD_CLS">
          <span :class="LABEL_CLS">布局</span>
          <Segmented
            :model-value="graph.config.layout"
            :options="LAYOUTS"
            size="sm"
            aria-label="图谱布局"
            @update:model-value="graph.patchConfig({ layout: $event })"
          />
        </div>

        <div :class="FIELD_CLS">
          <span :class="LABEL_CLS">着色</span>
          <Segmented
            :model-value="graph.config.colorBy"
            :options="COLOR_BY"
            size="sm"
            aria-label="节点着色维度"
            @update:model-value="graph.patchConfig({ colorBy: $event })"
          />
        </div>

        <RangeField
          :model-value="graph.config.repulsion"
          label="斥力"
          :min="100"
          :max="1200"
          :step="20"
          hint="越大节点越分散"
          @update:model-value="graph.patchConfig({ repulsion: $event })"
        />

        <RangeField
          :model-value="graph.config.gravity"
          label="向心力"
          :min="0"
          :max="0.5"
          :step="0.01"
          hint="越大越向中心收拢"
          @update:model-value="graph.patchConfig({ gravity: $event })"
        />

        <RangeField
          :model-value="graph.config.edgeLength[0]"
          label="边长下限"
          :min="10"
          :max="300"
          :step="5"
          @update:model-value="graph.patchConfig({ edgeLength: [$event, Math.max($event + 10, graph.config.edgeLength[1])] })"
        />

        <RangeField
          :model-value="graph.config.edgeLength[1]"
          label="边长上限"
          :min="20"
          :max="500"
          :step="10"
          @update:model-value="graph.patchConfig({ edgeLength: [Math.min($event - 10, graph.config.edgeLength[0]), $event] })"
        />

        <RangeField
          :model-value="graph.config.nodeScale"
          label="节点尺寸"
          :min="0.5"
          :max="2"
          :step="0.05"
          suffix="×"
          @update:model-value="graph.patchConfig({ nodeScale: $event })"
        />

        <RangeField
          :model-value="graph.config.edgeOpacity"
          label="连线透明度"
          :min="0.1"
          :max="1"
          :step="0.05"
          @update:model-value="graph.patchConfig({ edgeOpacity: $event })"
        />

        <RangeField
          :model-value="graph.config.minDegree"
          label="最小度数"
          :min="0"
          :max="20"
          hint="低于此度数的节点不画 —— 引文图里大量孤立点是纯噪音"
          @update:model-value="graph.patchConfig({ minDegree: $event })"
        />

        <RangeField
          :model-value="graph.config.labelMaxNodes"
          label="标签上限"
          :min="10"
          :max="200"
          :step="10"
          suffix=" 个"
          hint="节点数超过它就不再画标签，否则会变成一张灰纸"
          @update:model-value="graph.patchConfig({ labelMaxNodes: $event })"
        />

        <label class="flex cursor-pointer items-center gap-2 text-2xs text-ink-2">
          <input
            type="checkbox"
            class="size-3.5 accent-brand"
            :checked="graph.config.showLabels"
            @change="graph.patchConfig({ showLabels: ($event.target as HTMLInputElement).checked })"
          />
          显示节点标签
        </label>

        <label class="flex cursor-pointer items-center gap-2 text-2xs text-ink-2">
          <input
            type="checkbox"
            class="size-3.5 accent-brand"
            :checked="graph.config.highlightNeighbors"
            @change="graph.patchConfig({ highlightNeighbors: ($event.target as HTMLInputElement).checked })"
          />
          选中时高亮邻居
        </label>
      </aside>
    </div>

    <template #footer>
      <p class="flex items-center gap-1.5 px-2.5 py-1.5 text-2xs text-ink-4">
        <PhChartLine :size="11" />
        参数会记住（存本机）。后端按度数裁剪到 300 节点，此处只做展示层的再过滤。
      </p>
    </template>
  </AppPanel>

  <Modal :model-value="analysisOpen" :title="`图谱分析 · ${ANALYSES.find((a) => a.value === pickedAnalysis)?.label ?? ''}`" width="620px" @update:model-value="analysisOpen = false">
    <div v-if="graph.analyzing" class="flex items-center gap-2 py-6 text-[12.5px] text-ink-3">
      <AppSpinner :size="14" />
      正在分析…
    </div>
    <p v-else-if="graph.errorMessage" class="rounded-md bg-bad-soft px-2.5 py-2 text-2xs text-bad">
      {{ graph.errorMessage }}
    </p>
    <template v-else-if="graph.analysis">
      <p class="mb-2 text-2xs text-ink-3">
        {{ graph.analysis.n_nodes }} 节点 · {{ graph.analysis.n_edges }} 边
        <span v-if="graph.analysis.note"> · {{ graph.analysis.note }}</span>
      </p>
      <pre class="max-h-[52vh] overflow-auto scroll-slim rounded-md border border-hairline bg-sunken p-2.5 font-mono text-[11.5px] whitespace-pre-wrap text-ink-2">{{ analysisText }}</pre>
    </template>
  </Modal>
</template>
