<script setup lang="ts">
/**
 * 引文力导向图（ECharts）。
 *
 * 两个刻意的取舍：
 *
 * 1. **按需引入** `echarts/core` + GraphChart，而不是 `import * as echarts from 'echarts'`。
 *    后者会把整个 ECharts 打进包里（~1MB）。这里只用到 graph 一种图 + tooltip，
 *    按需引入后这块产物小一个数量级。
 *
 * 2. **选中态不重建 option**。ECharts 的 `emphasis.focus: 'adjacency'` 本身就是
 *    "高亮邻居、压暗其余"，正好是这个场景要的效果。用 dispatchAction 触发它，
 *    而不是重新 setOption —— 重建会让力导向重新模拟，用户点一下节点整张图就跳一遍。
 */
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { GraphEdge, GraphNode } from '~/types/api'
import type { GraphConfig } from '~/types/workbench'

echarts.use([GraphChart, TooltipComponent, CanvasRenderer])

const props = defineProps<{
  nodes: GraphNode[]
  edges: GraphEdge[]
  config: GraphConfig
  /** 当前选中节点 id。只驱动 highlight，不触发重建。 */
  selectedId: string | null
  maxInDegree: number
  maxPageRank: number
  maxDegree: number
}>()

const emit = defineEmits<{
  select: [id: string | null]
  /** 双击节点 —— 直接进 PDF 阅读器 */
  open: [id: string]
}>()

const el = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null
let resizeObserver: ResizeObserver | null = null
/** 缓存最后一次 setOption 用的节点顺序 —— dispatchAction 要的是 dataIndex，不是 id。 */
let orderedIds: string[] = []

/**
 * 社群配色。全部落在 500 级：
 * 浅色纸底上要够深才看得清，同时饱和度压住不要抢强调色。
 */
const COMMUNITY_PALETTE = [
  '#4a6cf7',
  '#159f5a',
  '#c9821a',
  '#c0432f',
  '#805ce5',
  '#0e8f99',
  '#bc4a8c',
  '#5c8a1e',
]

function cssVar(name: string, fallback: string): string {
  if (!import.meta.client) return fallback
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

/** 在两个 hex 之间线性插值，t ∈ [0,1]。用于按年份/度数做连续着色。 */
function mixHex(a: string, b: string, t: number): string {
  const parse = (h: string) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16))
  const [r1, g1, b1] = parse(a)
  const [r2, g2, b2] = parse(b)
  const c = (x: number, y: number) => Math.round(x + (y - x) * Math.min(1, Math.max(0, t)))
  return `rgb(${c(r1, r2)}, ${c(g1, g2)}, ${c(b1, b2)})`
}

function nodeColor(n: GraphNode): string {
  const cfg = props.config
  if (cfg.colorBy === 'community') {
    return COMMUNITY_PALETTE[(n.community ?? 0) % COMMUNITY_PALETTE.length]
  }
  if (cfg.colorBy === 'year') {
    const years = props.nodes.map((x) => x.year ?? 0).filter(Boolean)
    const min = Math.min(...years, 2000)
    const max = Math.max(...years, 2025)
    const t = max > min ? ((n.year ?? min) - min) / (max - min) : 0.5
    return mixHex('#c9b8a0', '#805ce5', t) // 旧 → 新：暖灰到品牌紫
  }
  const t = props.maxDegree > 0 ? (n.in_degree + n.out_degree) / props.maxDegree : 0
  return mixHex('#cfd4da', '#4a6cf7', t)
}

function buildOption(): echarts.EChartsCoreOption {
  const cfg = props.config
  const surface = cssVar('--color-surface', '#ffffff')
  const hairline = cssVar('--color-hairline-2', 'rgba(15,22,30,0.16)')
  const ink = cssVar('--color-ink', '#0f161e')
  const ink2 = cssVar('--color-ink-2', '#4a4f54')
  const brand = cssVar('--color-brand', '#805ce5')

  orderedIds = props.nodes.map((n) => n.id)

  return {
    tooltip: {
      confine: true,
      backgroundColor: surface,
      borderColor: hairline,
      borderWidth: 1,
      padding: [7, 9],
      extraCssText: 'box-shadow: 0 4px 12px rgb(15 22 30 / 0.08); border-radius: 8px;',
      textStyle: { color: ink, fontSize: 11.5 },
      formatter: (p: unknown) => {
        const d = p as { dataType: string; data: GraphNode }
        if (d.dataType !== 'node') return ''
        const n = d.data
        const esc = (s: string) => s.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c] as string)
        // 摘要压到 150 字：tooltip 是"扫一眼这篇讲什么"，不是读摘要的地方
        const abstract = (n.abstract ?? '').trim()
        const snippet = abstract.length > 150 ? `${abstract.slice(0, 150)}…` : abstract
        return [
          `<b>${esc(n.title || n.id)}</b>`,
          [n.year ? String(n.year) : '', n.venue ? esc(n.venue) : ''].filter(Boolean).join(' · '),
          `被引 ${n.in_degree} · 引用 ${n.out_degree}`,
          n.citation_count != null ? `总被引 ${n.citation_count}` : '',
          n.community != null ? `社群 ${n.community}` : '',
          n.pagerank != null ? `PageRank ${n.pagerank.toFixed(5)}` : '',
          snippet ? `<span style="opacity:.72">${esc(snippet)}</span>` : '',
        ]
          .filter(Boolean)
          .join('<br/>')
      },
    },
    series: [
      {
        type: 'graph',
        // force / circular / none 都由 ECharts 原生支持，不需要自己算布局
        layout: cfg.layout,
        roam: true,
        draggable: true,
        // 节点多的时候关标签：300 个节点的标签叠在一起就是一张灰纸
        label: {
          show: cfg.showLabels && props.nodes.length <= cfg.labelMaxNodes,
          position: 'right',
          fontSize: 10.5,
          color: ink2,
          formatter: (p: unknown) => {
            const t = String((p as { data: GraphNode }).data.title || '')
            return t.length > 18 ? `${t.slice(0, 18)}…` : t
          },
        },
        force: {
          repulsion: cfg.repulsion,
          edgeLength: cfg.edgeLength,
          gravity: cfg.gravity,
        },
        // 选中即高亮邻居：这是回答"这篇跟谁有关系"最快的视觉手段
        emphasis: {
          focus: cfg.highlightNeighbors ? 'adjacency' : 'none',
          scale: false,
          itemStyle: { borderColor: brand, borderWidth: 2.5 },
          label: { show: true, color: ink, fontSize: 11 },
        },
        lineStyle: { color: hairline, width: 1, curveness: 0.08, opacity: cfg.edgeOpacity },
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: 5,
        data: props.nodes.map((n) => ({
          id: n.id,
          name: n.title || n.id,
          title: n.title,
          year: n.year,
          community: n.community,
          in_degree: n.in_degree,
          out_degree: n.out_degree,
          pagerank: n.pagerank,
          // 大小同时反映被引数与 PageRank：被引多 = 领域基石，PageRank 高 = 枢纽
          symbolSize:
            (8 + 22 * (props.maxInDegree > 0 ? n.in_degree / props.maxInDegree : 0) +
              8 * (props.maxPageRank > 0 ? (n.pagerank ?? 0) / props.maxPageRank : 0)) *
            cfg.nodeScale,
          itemStyle: {
            color: nodeColor(n),
            // 描边用底色而不是纯白：和背景同色才能把重叠节点"切开"，又不出白圈
            borderColor: surface,
            borderWidth: 1.5,
          },
        })),
        edges: props.edges.map((e) => ({ source: e.source, target: e.target, value: e.weight })),
      },
    ],
  }
}

function rebuild() {
  if (!chart) return
  chart.setOption(buildOption(), { notMerge: true })
  syncSelection()
}

/** 把 store 里的选中同步到 ECharts 的 highlight 状态。 */
function syncSelection() {
  if (!chart) return
  chart.dispatchAction({ type: 'downplay', seriesIndex: 0 })
  const id = props.selectedId
  if (!id) return
  const idx = orderedIds.indexOf(id)
  if (idx >= 0) chart.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: idx })
}

function onSelect(id: string | null) {
  emit('select', id)
}

onMounted(() => {
  if (!el.value) return
  chart = echarts.init(el.value)
  chart.setOption(buildOption())
  chart.on('click', (params: unknown) => {
    const p = params as { dataType: string; data: { id?: string } }
    onSelect(p.dataType === 'node' ? (p.data?.id ?? null) : null)
  })
  // 双击 = 直接进 PDF。单击已经用来选中了，再让单击进阅读器会让"看一眼这个节点"
  // 变成"被迫离开图谱" —— 双击是这两件事共存的唯一分法。
  chart.on('dblclick', (params: unknown) => {
    const p = params as { dataType: string; data: { id?: string } }
    if (p.dataType === 'node' && p.data?.id) emit('open', p.data.id)
  })
  // 点空白处取消选中 —— 否则选中态只能靠再点另一个节点清掉
  chart.getZr().on('click', (e: unknown) => {
    const ev = e as { target?: unknown }
    if (!ev.target) onSelect(null)
  })
  syncSelection()
  resizeObserver = new ResizeObserver(() => chart?.resize())
  resizeObserver.observe(el.value)
})

watch(
  () => [props.nodes, props.edges, props.config],
  () => rebuild(),
  { deep: true },
)

watch(() => props.selectedId, syncSelection)

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  chart?.dispose()
  chart = null
})

defineExpose({ rebuild, resize: () => chart?.resize() })
</script>

<template>
  <div ref="el" class="h-full w-full" />
</template>
