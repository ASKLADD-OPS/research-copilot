<script setup lang="ts">
/**
 * 引文图（ECharts force 布局）。
 *
 * 后端已按度数裁剪到 300 节点（见 api/v1/graph.py），这里不再做聚合 ——
 * 前端的职责是"看清楚"，不是"再削一刀数据"。
 *
 * 颜色一律从 CSS token 现读（cssVar），不写死十六进制：换个主题整张图跟着换，
 * 否则暗色底上会浮出一张浅色配色的图。切换主题时重建一次 option。
 */
import * as echarts from 'echarts'
import type { GraphEdge, GraphNode } from '~/types/api'

const props = defineProps<{
  nodes: GraphNode[]
  edges: GraphEdge[]
  loading?: boolean
}>()

const emit = defineEmits<{ select: [node: GraphNode | null] }>()

const ui = useUiStore()
const el = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null

/** 读 :root 上的设计 token。SSR 阶段没有 DOM，返回空串由调用方兜底。 */
function cssVar(name: string): string {
  if (!import.meta.client) return ''
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

/**
 * 社群 → 颜色。500 级的一档：在 #010102（暗）和 #fbfbfc（浅）上都够对比度，
 * 不用为两套主题维护两份调色板。
 */
const PALETTE = ['#3b82f6', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#ec4899', '#84cc16']

function buildOption(): echarts.EChartsOption {
  const maxDeg = Math.max(1, ...props.nodes.map((n) => n.in_degree))
  const maxPr = Math.max(0.000001, ...props.nodes.map((n) => n.pagerank ?? 0))

  const surface = cssVar('--rc-surface-1')
  const hairline = cssVar('--rc-hairline-strong')
  const ink = cssVar('--rc-ink')
  const inkMuted = cssVar('--rc-ink-muted')

  return {
    tooltip: {
      confine: true,
      backgroundColor: surface,
      borderColor: hairline,
      borderWidth: 1,
      textStyle: { color: ink, fontSize: 12 },
      formatter: (p: unknown) => {
        const d = p as { dataType: string; data: Record<string, unknown> }
        if (d.dataType !== 'node') return ''
        const n = d.data as unknown as GraphNode & { symbolSize: number }
        return [
          `<b>${escapeHtml(n.title || n.id)}</b>`,
          `被引 ${n.in_degree} · 引用 ${n.out_degree}`,
          n.year ? `年份 ${n.year}` : '',
          n.community != null ? `社群 ${n.community}` : '',
          n.pagerank != null ? `PageRank ${n.pagerank.toFixed(5)}` : '',
        ]
          .filter(Boolean)
          .join('<br/>')
      },
    },
    legend: { show: false },
    series: [
      {
        type: 'graph',
        layout: 'force',
        roam: true,
        draggable: true,
        // 节点多的时候关掉标签，否则满屏字
        label: {
          show: props.nodes.length <= 40,
          position: 'right',
          fontSize: 11,
          color: inkMuted,
          formatter: (p: unknown) => truncate(String((p as { data: GraphNode }).data.title || ''), 18),
        },
        force: { repulsion: props.nodes.length > 120 ? 260 : 420, edgeLength: [40, 150], gravity: 0.08 },
        emphasis: { focus: 'adjacency', lineStyle: { width: 2 } },
        lineStyle: { color: hairline, width: 1, curveness: 0.08, opacity: 0.7 },
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: 6,
        data: props.nodes.map((n) => ({
          ...n,
          name: n.title || n.id,
          // 节点大小同时反映被引数与 PageRank：被引多 = 领域基石
          symbolSize: 8 + 22 * (n.in_degree / maxDeg) + 8 * ((n.pagerank ?? 0) / maxPr),
          itemStyle: {
            color: PALETTE[(n.community ?? 0) % PALETTE.length],
            // 描边用底色而不是纯白：暗色主题下白边会变成一圈噪点
            borderColor: surface,
            borderWidth: 1.5,
          },
        })),
        edges: props.edges.map((e) => ({ source: e.source, target: e.target, value: e.weight })),
      },
    ],
  }
}

function escapeHtml(s: string) {
  return s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] as string)
}
function truncate(s: string, n: number) {
  return s.length > n ? `${s.slice(0, n)}…` : s
}

function resize() {
  chart?.resize()
}

function resetView() {
  chart?.setOption(buildOption(), { notMerge: true })
}

onMounted(() => {
  if (!el.value) return
  chart = echarts.init(el.value)
  chart.setOption(buildOption())
  chart.on('click', (params: unknown) => {
    const p = params as { dataType: string; data: GraphNode }
    emit('select', p.dataType === 'node' ? p.data : null)
  })
  window.addEventListener('resize', resize)
})

watch(
  () => [props.nodes, props.edges],
  () => chart?.setOption(buildOption(), { notMerge: true }),
)

// 主题换了，token 的值也换了 —— 图必须重建，不然配色留在旧主题上
watch(() => ui.theme, () => resetView())

onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  chart?.dispose()
  chart = null
})

defineExpose({ resetView, resize })
</script>

<template>
  <div class="graph-wrap rc-panel">
    <div ref="el" class="canvas" />
    <div v-if="loading" class="busy">
      <span class="rc-spin" />
      <span class="rc-caption">正在布局…</span>
    </div>
    <div class="hint rc-caption">
      滚轮缩放 · 拖拽平移 · 点击节点看详情 · 箭头方向 = 引用方向（citing → cited）
    </div>
  </div>
</template>

<style scoped>
.graph-wrap {
  position: relative;
  height: 100%;
  overflow: hidden;
}
.canvas {
  height: 100%;
  width: 100%;
}

.busy {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: color-mix(in srgb, var(--rc-canvas) 55%, transparent);
}

.hint {
  position: absolute;
  left: 10px;
  bottom: 8px;
  padding: 2px 8px;
  border-radius: var(--rc-radius-sm);
  background: color-mix(in srgb, var(--rc-surface-2) 86%, transparent);
}
</style>
