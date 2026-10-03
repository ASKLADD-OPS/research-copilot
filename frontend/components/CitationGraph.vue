<script setup lang="ts">
/**
 * 引文图谱 —— ECharts graph（force）+ 时间轴 + 社区图例 + 导出。
 *
 * 组件自己有四个状态，因为它们是"这一张图"的，不是全局长相：
 * 年份区间、被隐藏的社区、已展开的节点。`config` 只管长相（斥力/边长/着色/尺寸）。
 *
 * 三个刻意的取舍：
 *
 * 1. **只挂 SVG 渲染器**，不挂 canvas。原因不是好看，是导出：
 *    ECharts 没有"把 canvas 实例转成矢量"的公开 API（`renderToSVGString` 在 dev 下
 *    对 canvas 实例直接抛错），想在 canvas 上导 SVG 就得另起一个实例把节点坐标抄过去
 *    再序列化 —— 多一份实例、多一份坐标同步、还丢了用户当前的缩放平移。
 *    统一走 SVG 后，`renderToSVGString()` 出来的就是屏幕上那一张（含 roam 变换），
 *    PNG 则把这个 SVG 画进 canvas 导出。代价是 >200 节点时 SVG 的 DOM 开销高于 canvas，
 *    所以那一档要摘标签/收细连线（见 degraded）。
 *
 * 2. **更新一律 merge（`notMerge: false`）**。ECharts 的力导向把算出来的坐标存在
 *    `seriesModel.preservedPoints` 上，merge 时这张表还在，于是点开一个邻居只会让新节点
 *    自己飞进来，已摆好的那部分纹丝不动。notMerge 会丢掉它 —— 每次交互整张图重排一次，
 *    就是"点一下整个画布跳一下"的来源。只有「重置」才故意 notMerge。
 *
 * 3. **hover 放大用原生的 `emphasis.scale`**，不去自己监听 mouseover 改 symbolSize：
 *    后者每次都要重设 option，力导向会跟着重跑一遍模拟。加粗描边 + 提亮就是为了让
 *    1.1 倍的放大看得出来。
 */
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'
import {
  PhArrowsCounterClockwise,
  PhCalendarBlank,
  PhFileSvg,
  PhImage,
  PhMagnifyingGlassMinus,
  PhMagnifyingGlassPlus,
} from '@phosphor-icons/vue'
import type { GraphEdge, GraphNode } from '~/types/api'
import type { GraphColorBy, GraphConfig } from '~/types/workbench'

echarts.use([GraphChart, TooltipComponent, SVGRenderer])

const props = defineProps<{
  /** 全量节点（不做年份/度数过滤）—— 展开邻居时要能查到被过滤掉的节点 */
  nodes: GraphNode[]
  edges: GraphEdge[]
  config: GraphConfig
  /** 当前选中节点 id。只驱动 highlight，不触发重建 */
  selectedId: string | null
  yearFrom: number | null
  yearTo: number | null
}>()

const emit = defineEmits<{
  select: [id: string | null]
  /** 双击节点 —— 直接进 PDF 阅读器 */
  open: [id: string]
  'update:yearFrom': [value: number | null]
  'update:yearTo': [value: number | null]
  /** 当前真正画出来的规模，面板标题用 */
  stats: [value: { nodes: number; edges: number }]
}>()

/** 超过这个节点数就摘掉标签、收细连线。ECharts 的 graph 系列**不支持 progressive**
 *  （源码 `circularLayoutHelper.js` 里写明了 "progressive rendering is not applied to
 *  graph"），所以顶用的不是 progressive 而是这个降级开关。
 *  下面仍然把 progressive 写进 option：ECharts 接受这两个字段，哪天 graph 支持了直接生效。 */
const DEGRADE_AT = 200

/** ColorBrewer 定性色板：Dark2(8) + Set1/Set3 里够深的几个。白底上要够饱和才分得开社区。 */
const BREWER = [
  '#1b9e77', '#d95f02', '#7570b3', '#e7298a', '#66a61e', '#e6ab02', '#a6761d', '#666666',
  '#377eb8', '#e41a1c', '#4daf4a', '#984ea3', '#ff7f00', '#a65628', '#f781bf', '#bc80bd',
]

const library = useLibraryStore()

const el = ref<HTMLElement | null>(null)
const chartReady = ref(false)
let chart: echarts.ECharts | null = null
let resizeObserver: ResizeObserver | null = null
/** 最后一次 setOption 的节点顺序 —— dispatchAction 要的是 dataIndex，不是 id */
let orderedIds: string[] = []

/** 被隐藏的社区 id */
const hidden = ref<Set<number>>(new Set())
/** 已展开的节点 id（点过它的邻居已经补进图里） */
const expanded = ref<Set<string>>(new Set())

// ---------------------------------------------------------------- 过滤

const degree = (n: GraphNode) => n.degree ?? n.in_degree + n.out_degree

const yearBounds = computed(() => {
  const years = props.nodes.map((n) => n.year).filter((y): y is number => typeof y === 'number')
  return years.length ? { min: Math.min(...years), max: Math.max(...years) } : null
})

const yearFiltered = computed(() => props.yearFrom !== null || props.yearTo !== null)

function inYears(n: GraphNode) {
  if (typeof n.year !== 'number') return !yearFiltered.value
  if (props.yearFrom !== null && n.year < props.yearFrom) return false
  if (props.yearTo !== null && n.year > props.yearTo) return false
  return true
}

const alive = (n: GraphNode) => !hidden.value.has(n.community ?? -1)

/** 过了年份/度数/社区三道刀、真正基线会画出来的节点。 */
const baseNodes = computed(() =>
  props.nodes.filter((n) => inYears(n) && alive(n) && degree(n) >= props.config.minDegree),
)

/** 展开邻居时"补回来"的节点：被上面过滤掉、但邻居被点开过。
 *  这是"点击展开邻居 = 动态添加节点"落到实处的地方 —— 没被过滤时它自然为空。 */
const revivedNodes = computed(() => {
  if (!expanded.value.size) return []
  const shown = new Set(baseNodes.value.map((n) => n.id))
  const wanted = new Set<string>()
  for (const e of props.edges) {
    if (expanded.value.has(e.source)) wanted.add(e.target)
    if (expanded.value.has(e.target)) wanted.add(e.source)
  }
  return props.nodes.filter((n) => wanted.has(n.id) && !shown.has(n.id) && alive(n))
})

const renderNodes = computed(() => [...baseNodes.value, ...revivedNodes.value])

const renderEdges = computed(() => {
  const ids = new Set(renderNodes.value.map((n) => n.id))
  return props.edges.filter((e) => ids.has(e.source) && ids.has(e.target))
})

const maxPageRank = computed(() =>
  Math.max(1e-9, ...renderNodes.value.map((n) => n.pagerank ?? 0)),
)
const maxDegree = computed(() => Math.max(1, ...renderNodes.value.map(degree)))
const maxWeight = computed(() => Math.max(1e-9, ...renderEdges.value.map((e) => e.weight || 0)))

const degraded = computed(() => renderNodes.value.length > DEGRADE_AT)

/** 标签只给核心的那几篇：全画出来就是一张灰纸，画 0 张又读不懂图。 */
const labelIds = computed(() => {
  if (!props.config.showLabels || degraded.value) return new Set<string>()
  const top = [...renderNodes.value]
    .sort((a, b) => (b.pagerank ?? 0) - (a.pagerank ?? 0))
    .slice(0, Math.max(0, props.config.labelMaxNodes))
  return new Set(top.map((n) => n.id))
})

/** 社区清单。用**全量**节点统计，这样隐藏一个社区后其他社区的计数不会跟着跳。 */
const communities = computed(() => {
  const map = new Map<number, number>()
  for (const n of props.nodes) {
    const k = n.community ?? -1
    map.set(k, (map.get(k) ?? 0) + 1)
  }
  return [...map.entries()]
    .map(([id, count]) => ({ id, count, color: communityColor(id) }))
    .sort((a, b) => b.count - a.count)
})

// ---------------------------------------------------------------- 着色 / 尺寸

function communityColor(id: number | null | undefined) {
  // -1 = 没算出社区。固定给灰色，免得它跟 0 号社区撞色
  if (id == null || id < 0) return '#b4b8bd'
  return BREWER[id % BREWER.length]
}

function nodeColor(n: GraphNode) {
  const by: GraphColorBy = props.config.colorBy
  if (by === 'community') return communityColor(n.community)
  if (by === 'year') {
    const b = yearBounds.value
    if (!b) return communityColor(n.community)
    const t = b.max > b.min ? ((n.year ?? b.min) - b.min) / (b.max - b.min) : 0.5
    return mixHex('#c9b8a0', '#805ce5', t)
  }
  return mixHex('#cfd4da', '#4a6cf7', degree(n) / maxDegree.value)
}

/** 在两个 hex 之间线性插值，t ∈ [0,1] */
function mixHex(a: string, b: string, t: number): string {
  const parse = (h: string) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16))
  const [r1, g1, b1] = parse(a)
  const [r2, g2, b2] = parse(b)
  const k = (x: number, y: number) => Math.round(x + (y - x) * Math.min(1, Math.max(0, t)))
  return `rgb(${k(r1, r2)}, ${k(g1, g2)}, ${k(b1, b2)})`
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

/**
 * 节点大小 = PageRank × 100，夹在 [10, 50]。
 *
 * 先按本图最大值归一化再 ×100：后端的 pagerank 是 networkx 的原始值（全图求和 = 1），
 * 20 篇时每篇约 0.05，直接 ×100 会得到 5，被下限 10 压成一片同样大的点 ——
 * 归一化之后 100 才落在"最有影响力的那篇"上，尺寸差才看得出来。
 */
function nodeSize(n: GraphNode) {
  const t = (n.pagerank ?? 0) / maxPageRank.value
  const base = clamp(Math.round(t * 100), 10, 50)
  return (degraded.value ? base * 0.7 : base) * props.config.nodeScale
}

/** 引用强度 → 线宽。强引用是最粗那条的 3 倍左右，再粗就糊成一片。 */
function edgeWidth(e: GraphEdge) {
  if (degraded.value) return 0.8
  return 0.7 + 2.3 * ((e.weight || 0) / maxWeight.value)
}

/** 连线的颜色取自**源节点**（谁在引用），透明度跟着强度走。
 *  没有用真正的线性渐变对象：graph 的边在布局前拿不到两端坐标，渐变坐标系写死了也只会
 *  退化成单色。强度的差异改由"线宽 + 透明度"表达，读起来是一回事，且一定不会画歪。 */
function edgeOpacity(e: GraphEdge) {
  const t = (e.weight || 0) / maxWeight.value
  return clamp(0.16 + 0.5 * t, 0, 1) * props.config.edgeOpacity
}

/** 作者只在**前端已加载的文献库**里查：图谱载荷没有作者字段，为 tooltip 上一行字
 *  去改后端 schema + 建图器不划算。查不到就不显示这一行。 */
function authorLine(id: string) {
  const names = (library.byId.get(id)?.authors ?? []).map((a) => a.name).filter(Boolean)
  if (!names.length) return ''
  return names.length > 3 ? `${names.slice(0, 3).join(', ')} 等 ${names.length} 人` : names.join(', ')
}

function cssVar(name: string, fallback: string) {
  if (!import.meta.client) return fallback
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

// ---------------------------------------------------------------- option

function buildOption(): echarts.EChartsCoreOption {
  const cfg = props.config
  const surface = cssVar('--color-surface', '#ffffff')
  const hairline = cssVar('--color-hairline-2', 'rgba(15,22,30,0.16)')
  const ink = cssVar('--color-ink', '#0f161e')
  const ink2 = cssVar('--color-ink-2', '#4a4f54')
  const brand = cssVar('--color-brand', '#805ce5')

  orderedIds = renderNodes.value.map((n) => n.id)
  const colors = new Map(renderNodes.value.map((n) => [n.id, nodeColor(n)]))
  const small = degraded.value

  return {
    // 导出时这张底会被带进 PNG/SVG —— 报告里要的是纸白，不是透明
    backgroundColor: surface,
    stateAnimation: { duration: 140 },
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
        const authors = authorLine(n.id)
        const abstract = (n.abstract ?? '').trim()
        const snippet = abstract.length > 120 ? `${abstract.slice(0, 120)}…` : abstract
        return [
          `<b>${esc(n.title || n.id)}</b>`,
          esc(authors),
          [n.year ? String(n.year) : '', n.venue ? esc(n.venue) : ''].filter(Boolean).join(' · '),
          `被引 ${n.in_degree} · 引用 ${n.out_degree}${n.citation_count != null ? ` · 总被引 ${n.citation_count}` : ''}`,
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
        layout: cfg.layout,
        roam: true,
        draggable: true,
        // graph 不支持 progressive，这两个字段现在是空转；见 DEGRADE_AT 的说明
        progressive: 400,
        progressiveThreshold: 300,
        animationDuration: 500,
        animationEasingUpdate: 'cubicOut',
        // 斥力 300 / 边长 50–150 / 向心 0.1：这三项是"20 篇能看清、50 篇不糊"的那组
        force: {
          repulsion: cfg.repulsion,
          edgeLength: cfg.edgeLength,
          gravity: cfg.gravity,
          friction: 0.6,
        },
        label: {
          position: 'right',
          distance: 4,
          fontSize: 10.5,
          color: ink2,
          // 描边用底色当"晕"，压在连线上也能读清；比加底板轻
          textBorderColor: surface,
          textBorderWidth: 2.5,
          formatter: (p: unknown) => {
            const t = String((p as { data: GraphNode }).data.title || '')
            return t.length > 18 ? `${t.slice(0, 18)}…` : t
          },
        },
        // 选中即高亮邻居：回答"这篇跟谁有关系"最快的视觉手段。scale 是原生放大
        emphasis: {
          focus: cfg.highlightNeighbors ? 'adjacency' : 'none',
          scale: true,
          itemStyle: { borderColor: brand, borderWidth: 2.5 },
          label: { show: true, color: ink, fontSize: 11, textBorderColor: surface, textBorderWidth: 2.5 },
        },
        blur: {
          // 压暗但不至于"整张图消失"：0.3 上下还能看出图的形状，再低就只剩一个亮点了
          itemStyle: { opacity: 0.32 },
          lineStyle: { opacity: 0.12 },
          label: { opacity: 0.12 },
        },
        // 10 个标签里总有两三个会撞在一起或顶出画布边缘，撞了就藏掉而不是硬画
        labelLayout: { hideOverlap: true },
        lineStyle: { curveness: 0.08 },
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: small ? 4 : 5,
        data: renderNodes.value.map((n) => ({
          id: n.id,
          // name 只作为 ECharts 内部的节点标识（边按 id 解析），label 显示的是 title
          name: n.id,
          title: n.title || n.id,
          year: n.year,
          venue: n.venue,
          // tooltip 要用的字段必须显式带进 data —— ECharts 只把进 data 的字段原样交回
          // 给 formatter，漏传就静默渲染成空。
          citation_count: n.citation_count,
          abstract: n.abstract,
          community: n.community,
          in_degree: n.in_degree,
          out_degree: n.out_degree,
          pagerank: n.pagerank,
          symbolSize: nodeSize(n),
          label: { show: labelIds.value.has(n.id) },
          itemStyle: {
            color: colors.get(n.id),
            // 描边用底色而不是纯白：和背景同色才能把重叠节点"切开"，又不出白圈
            borderColor: surface,
            borderWidth: small ? 0 : 1.5,
          },
        })),
        edges: renderEdges.value.map((e) => ({
          source: e.source,
          target: e.target,
          value: e.weight,
          lineStyle: {
            color: colors.get(e.source) ?? hairline,
            width: edgeWidth(e),
            opacity: edgeOpacity(e),
            curveness: 0.08,
          },
        })),
      },
    ],
  }
}

// ---------------------------------------------------------------- 渲染

/** merge 保留已摆好的坐标（见文件头 2），只有重置才 notMerge */
function apply(opts: { fresh?: boolean } = {}) {
  if (!chart) return
  chart.setOption(buildOption(), { notMerge: opts.fresh === true })
  syncSelection()
  scheduleFit()
  emit('stats', { nodes: renderNodes.value.length, edges: renderEdges.value.length })
}

function syncSelection() {
  if (!chart) return
  chart.dispatchAction({ type: 'downplay', seriesIndex: 0 })
  const id = props.selectedId
  if (!id) return
  const idx = orderedIds.indexOf(id)
  if (idx >= 0) chart.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: idx })
}

// ---------------------------------------------------------------- 自适应缩放

/** ECharts 把算好的节点坐标存在 series 的 data 上，只有 getModel() 能读到，
 *  而它在 .d.ts 里被标成 private（运行时有）。这里收窄成一个只用到两个方法的最小接口。 */
interface SeriesDataLike {
  count(): number
  getItemLayout(i: number): unknown
}

function seriesModel(): { getData?: () => SeriesDataLike; coordinateSystem?: ViewLike } | undefined {
  const model = (
    chart as unknown as {
      getModel?: () => {
        getSeriesByIndex?: (
          i: number,
        ) => { getData?: () => SeriesDataLike; coordinateSystem?: ViewLike } | undefined
      }
    }
  )?.getModel?.()
  return model?.getSeriesByIndex?.(0)
}

function seriesData(): SeriesDataLike | null {
  return seriesModel()?.getData?.() ?? null
}

/** 同上，`coordinateSystem` 运行时挂在 seriesModel 上、但没进 .d.ts。
 *  ⚠ 它返回的是**已含缩放的**画布像素：我们把每次缩放都同时派发了 `graphRoam`，
 *  而该动作会写回 `view.scaleX/x` 并 `updateTransform()`（action/roamHelper.js
 *  `updateCenterAndZoom`）—— 坐标系和 group 始终同步，所以这里量到的就是眼前所见。 */
interface ViewLike {
  dataToPoint?: (data: number[]) => number[]
}

/** 只量不改：节点云的**已绘制**包围盒（圆心 ± symbolSize/2，与 getItemLayout 同一空间）。
 *  必须把半径算进去：缩放乘在 group 上、半径也跟着放大，而 fit 量的是**圆心** ——
 *  按圆心算出的倍率会稳定偏大 (span+2r)/span。实测 20 篇 @778x518 溢出 3~6%
 *  （spread 0.87x1.03 / 1.00x1.06），而这正是"最边上那个节点被裁掉一截"的来源。 */
function measure(): { n: number; box: [number, number, number, number] } | null {
  const data = seriesData()
  if (!data) return null
  const rs = renderNodes.value
  let x0 = Infinity
  let y0 = Infinity
  let x1 = -Infinity
  let y1 = -Infinity
  let n = 0
  for (let i = 0; i < data.count(); i++) {
    const p = data.getItemLayout(i) as number[] | undefined
    if (!Array.isArray(p) || !Number.isFinite(p[0]) || !Number.isFinite(p[1])) continue
    // series data 就是 renderNodes 的 map，顺序一一对应
    const r = (rs[i] ? nodeSize(rs[i]!) : 20) / 2
    x0 = Math.min(x0, p[0] - r)
    x1 = Math.max(x1, p[0] + r)
    y0 = Math.min(y0, p[1] - r)
    y1 = Math.max(y1, p[1] + r)
    n++
  }
  if (n < 2) return null
  return { n, box: [x0, y0, x1, y1] }
}

/** series group：zrender 的可变换容器。p' = group.x + group.scaleX * p。 */
interface GroupLike {
  x: number
  y: number
  scaleX: number
  scaleY: number
  dirty: () => void
}
interface GraphViewLike {
  group?: GroupLike
}

function graphView(): GraphViewLike | null {
  const gv = (
    chart as unknown as { getViewOfSeriesModel?: (m: unknown) => GraphViewLike | undefined }
  )?.getViewOfSeriesModel?.(seriesModel())
  return gv ?? null
}

/**
 * 视图缩放/平移必须**同时改两处**，和 GraphView._updateController 里滚轮那一支完全对应：
 *   ① series `group` 的 transform —— 立刻看得见的那次移动；
 *   ② `graphRoam` 动作 —— 把同样的值同步进坐标系与 series 模型（`update:'none'`，不重绘）。
 * 只做 ①：下次重绘（换筛选条件、窗口 resize）GraphView.render 会拿坐标系里的旧值
 * `group.attr({x: coordSys.x, scaleX: coordSys.scaleX, ...})` 把 group 覆盖回去，视图被弹回原位。
 * 只做 ②（最初的写法）：画面**纹丝不动** —— 实测点两次『放大』，节点云占画布的比例一点没变，
 * 而 fit-diag 明明算出了 1.66 倍。
 * 两处用的是同一套公式（在 (px,py) 处乘 factor），所以不会互相打架。
 */
function roamSilently(act: () => void) {
  fitting = true
  try {
    act()
  } finally {
    fitting = false
  }
}

function zoomAt(factor: number, px: number, py: number) {
  const g = graphView()?.group
  if (!g || !Number.isFinite(factor) || factor <= 0 || Math.abs(factor - 1) < 1e-3) return
  roamSilently(() => {
    g.x -= (px - g.x) * (factor - 1)
    g.y -= (py - g.y) * (factor - 1)
    g.scaleX *= factor
    g.scaleY *= factor
    g.dirty()
    chart?.dispatchAction({ type: 'graphRoam', seriesIndex: 0, zoom: factor, originX: px, originY: py })
  })
}

function panBy(dx: number, dy: number) {
  const g = graphView()?.group
  if (!g || (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5)) return
  roamSilently(() => {
    g.x += dx
    g.y += dy
    g.dirty()
    chart?.dispatchAction({ type: 'graphRoam', seriesIndex: 0, dx, dy })
  })
}

/**
 * 力导向的平衡尺度只由节点数决定：20 篇会缩成画布中间一小团，240 篇会漫到画布外。
 * 需求把 repulsion / edgeLength / gravity 三个值钉死了，所以只能补一次**视图**缩放 ——
 * 双向的：小图放大铺满，大图缩小看得全（下限 0.3，不是 1）。
 * 早先按"只放大不缩小"钳在 ≥1，结果 240 篇那档一路顶着画布边缘被裁掉，而 fit 明明
 * 每跳都在跑、却永远算出 1（上下都受限，min 取到画布高/云高 = 0.4 → 被钳回 1）。
 *
 * 是**幂等**的（按当前实际像素算出目标倍率，不是累加），所以每跳都可以放心调一次：
 * 视图会跟着力导向一起长大，收敛那一跳给出的就是精确结果 —— 把 span = scale × baseSpan
 * 代进去，target = 可用区 / baseSpan，与当前 scale 无关，一步就落到不动点。
 * `dataToPoint` 返回的是**当前**（含缩放）像素，所以量出来的尺寸可以直接用，不用再折算。
 */
function fitStep(m: { n: number; box: [number, number, number, number] }) {
  const g = graphView()?.group
  if (!chart || !g) return null
  const w = chart.getWidth()
  const h = chart.getHeight()
  if (!w || !h) return null
  const [x0, y0, x1, y1] = m.box
  // 必须保持 view.xxx(...) 的调用形式：View.prototype.dataToPoint 内部读 this.transform，
  // 把方法解构出来单独调（const toPx = view.dataToPoint）this 就是 undefined，
  // 整个页面会挂成 500 —— "Cannot read properties of undefined (reading 'transform')"。
  const view = seriesModel()?.coordinateSystem
  const p0 = view?.dataToPoint?.([x0, y0])
  const p1 = view?.dataToPoint?.([x1, y1])
  const spanW = Math.max(1, Math.abs((p1?.[0] ?? x1) - (p0?.[0] ?? x0)))
  const spanH = Math.max(1, Math.abs((p1?.[1] ?? y1) - (p0?.[1] ?? y0)))
  // 包围盒已经是"画出来的范围"（含节点半径），这里只需要留一圈视觉余量。
  // 24 而不是贴着边：力导向在 fit 停手之后还可能再胀 2~3%（无焦点标签页里 rAF 被节流时
  // 尤其明显，实测最后一次测量比终态小 2.8%），这点余量刚好把它吃掉 ——
  // 否则表现就是"最外圈那圈节点被画布边缘切掉一小截"，而 spread 只差 0.02 看不出来。
  const pad = 24
  // 目标倍率换算成"当前倍率的比例"。上下限都得有：下限不是 1。
  const target = clamp(
    g.scaleX * Math.min((w - pad * 2) / spanW, (h - pad * 2) / spanH),
    0.3,
    3.5,
  )
  zoomAt(target / g.scaleX, w / 2, h / 2)
  // dataToPoint 给的就是**当前画布像素**（已含 group 变换），所以云心直接和画布中心相减。
  // 再乘一遍 g.scaleX 加 g.x 就等于把变换算两遍 —— 实测会稳定偏出 246px 且不收敛
  // （每跳都算出同一个错的 dx，于是停在一个"错公式的不动点"上）。
  const c = view?.dataToPoint?.([(x0 + x1) / 2, (y0 + y1) / 2])
  panBy(w / 2 - (c?.[0] ?? (x0 + x1) / 2), h / 2 - (c?.[1] ?? (y0 + y1) / 2))
  return {
    n: m.n,
    box: m.box.map(Math.round),
    zoom: Math.round(g.scaleX * 100) / 100,
    canvas: [w, h],
  }
}

/** 诊断用：当前视图倍率。自适应缩放到底生效没有，看这个比量包围盒直接。 */
function viewScale(): number {
  return Math.round((graphView()?.group?.scaleX ?? 1) * 100) / 100
}

/** 按内容自适应缩放并居中。返回诊断信息（自检页/控制台用）。 */
function fitView() {
  // 用户自己缩过/拖过就别再抢镜头了 —— 展开一个邻居不该把视图拉回原位
  if (roamed) return null
  const m = measure()
  return m ? fitStep(m) : null
}

/**
 * 力导向要好几秒才收敛，而且**中途比终态更大**：simpleLayout 先把节点铺满
 * 整个视图矩形，再被引力收拢。所以在固定时刻量一次必然偏大 → zoom 偏小：实测 2.6s 量到
 * zoom≈1.2，等布局收拢到终态就只填满画布一半（0.36x0.52），而终态该是 1.9（0.67x0.97）。
 * 全程只读 getItemLayout、不改 option，所以探测本身零副作用，也不会像 setOption 那样
 * 把布局反复退火。
 *
 * **退出只看跳数上限，不做"量到不再变"的提前退出。** 试过 <4px 的抖动阈值，它会在还有
 * ~3% 余量时就提前收工：力导向是**渐近**收敛的，读数间隔 420ms 时先掉到 4px 以下、
 * 之后几十秒里继续慢慢爬（实测最后一跳量到的包围盒比终态小 2.8% —— 同一个公式事后
 * 手动再 fit 一次，倍率从 1.15 变成 1.11）。这点误差在路径并集盒上只差 0.96→0.97，
 * 靠肉眼看 spread 根本发现不了，但表现出来就是最外圈节点被边缘切掉一点。
 * 多跑几跳的代价约等于零：fitStep 幂等、只读布局 + 改视图，10 秒内几十次 O(n) 读取；
 * 用户一旦自己动视图（`roamed`）立即停手。
 */
const FIT_TICKS = 40
const FIT_STEP = 420
let fitTimer: ReturnType<typeof setTimeout> | null = null
let fitTicks = 0

function scheduleFit() {
  if (fitTimer) clearTimeout(fitTimer)
  fitTimer = null
  fitTicks = 0
  tickFit()
}

function tickFit() {
  fitTicks++
  const m = measure()
  // 每跳都调：fitStep 幂等且只改视图（不 setOption、不重启力导向），
  // 于是视图跟着布局一起长大，最后一跳给出的就是当下的精确值。
  if (m && !roamed) fitStep(m)
  if (fitTicks >= FIT_TICKS) return
  fitTimer = setTimeout(tickFit, FIT_STEP)
}

/** 用户动过视图（滚轮/拖拽/工具栏缩放）后置位，重排不再自动抢镜头；重置时清掉。 */
let roamed = false

/** 正在由 fitStep/zoomAt 自己改视图 —— 别把自己发的 graphRoam 当成"用户动过视图"。 */
let fitting = false

function fit() {
  chart?.resize()
  roamed = false
  apply({ fresh: true })
}

/** 重置：重排布局 + 回到 1 倍缩放 + 清掉展开与选中 */
function resetView() {
  expanded.value = new Set()
  emit('select', null)
  fit()
}

function zoomBy(factor: number) {
  if (!chart) return
  // 锚在画布中心：这是"放大/缩小"该有的手感，也是滚轮默认的锚点。
  // 走 roamSilently 才不会被自己的 graphRoam 反过来当成用户操作，那这里就手动置位。
  roamed = true
  zoomAt(factor, chart.getWidth() / 2, chart.getHeight() / 2)
}

// ---------------------------------------------------------------- 交互

function neighborsOf(id: string): string[] {
  const out: string[] = []
  for (const e of props.edges) {
    if (e.source === id) out.push(e.target)
    if (e.target === id) out.push(e.source)
  }
  return out
}

/** 单击 = 选中 + 展开邻居。展开会把被年份/社区过滤掉的邻居**补回图上**，
 *  于是"顺着一篇往下看"不用来回改滑杆；没被过滤时它就是一次纯粹的高亮。 */
function toggleExpand(id: string) {
  const next = new Set(expanded.value)
  if (next.has(id)) {
    next.delete(id)
    for (const n of neighborsOf(id)) next.delete(n)
  } else {
    next.add(id)
    for (const n of neighborsOf(id)) next.add(n)
  }
  expanded.value = next
}

function toggleCommunity(id: number) {
  const next = new Set(hidden.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  hidden.value = next
  apply()
}

function setYear(which: 'from' | 'to', value: number) {
  if (which === 'from') {
    // 顺手把上界顶上去：出现"起 > 止"的空区间只会让用户以为图坏了
    emit('update:yearFrom', value)
    if (props.yearTo !== null && value > props.yearTo) emit('update:yearTo', value)
  } else {
    emit('update:yearTo', value)
    if (props.yearFrom !== null && value < props.yearFrom) emit('update:yearFrom', value)
  }
}

function clearYears() {
  emit('update:yearFrom', null)
  emit('update:yearTo', null)
}

// ---------------------------------------------------------------- 导出

function stamp() {
  return new Date().toISOString().slice(0, 10)
}

function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  // 立刻 revoke 在部分浏览器上会把还没开始读的下载掐掉
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/** 当前画布的矢量快照 —— 导出 SVG / 栅格化成 PNG 都走它。 */
function svgString(): string {
  if (!chart) return ''
  return chart.renderToSVGString({ useViewBox: true })
}

/** SVG 导出：直接序列化当前 painter，导出的是屏幕上这一张（含缩放平移）。 */
function exportSvg() {
  const svg = svgString()
  if (!svg) return
  saveBlob(new Blob([svg], { type: 'image/svg+xml;charset=utf-8' }), `citation-graph-${stamp()}.svg`)
}

/** PNG 导出：SVG 渲染器没有栅格化能力，把矢量串画进 2× canvas 再导。 */
async function exportPng() {
  if (!chart) return
  const svg = svgString()
  if (!svg) return
  const img = new Image()
  img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
  await img.decode()
  const ratio = 2
  const canvas = document.createElement('canvas')
  canvas.width = chart.getWidth() * ratio
  canvas.height = chart.getHeight() * ratio
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
  canvas.toBlob((blob) => blob && saveBlob(blob, `citation-graph-${stamp()}.png`), 'image/png')
}

// ---------------------------------------------------------------- 生命周期

onMounted(() => {
  if (!el.value) return
  chart = echarts.init(el.value, null, { renderer: 'svg' })
  chart.setOption(buildOption())
  chartReady.value = true
  // 首屏这一发 setOption 走的是这里、不是 apply()，所以自适应缩放得单独喊一声 ——
  // 少了这行，自动缩放只有等某个 watcher 触发过 apply() 才会生效
  scheduleFit()

  chart.on('click', (params: unknown) => {
    const p = params as { dataType?: string; data?: { id?: string } }
    if (p.dataType !== 'node' || !p.data?.id) return
    emit('select', p.data.id)
    toggleExpand(p.data.id)
    apply()
  })

  // 双击 = 直接进 PDF。单击已经用来选中+展开了，再让单击进阅读器会让"看一眼这个节点"
  // 变成"被迫离开图谱" —— 双击是这两件事共存的唯一分法。
  chart.on('dblclick', (params: unknown) => {
    const p = params as { dataType?: string; data?: { id?: string } }
    if (p.dataType === 'node' && p.data?.id) emit('open', p.data.id)
  })

  // 点空白处取消选中，否则选中态只能靠再点另一个节点清掉
  chart.getZr().on('click', (e: unknown) => {
    if (!(e as { target?: unknown }).target) emit('select', null)
  })

  // 滚轮/拖拽（以及工具栏缩放）都会派发这个动作 —— 之后就别再自动抢镜头了。
  // 注意：真正的缩放发生在 group 上（见 zoomAt），这个事件只同步内部状态。
  chart.on('graphroam', () => {
    if (!fitting) roamed = true
  })

  syncSelection()
  emit('stats', { nodes: renderNodes.value.length, edges: renderEdges.value.length })
  resizeObserver = new ResizeObserver(() => chart?.resize())
  resizeObserver.observe(el.value)
})

watch(
  () => [props.nodes, props.edges, props.config, props.yearFrom, props.yearTo],
  () => apply(),
  { deep: true },
)

watch(() => props.selectedId, syncSelection)

watch(
  () => props.nodes.map((n) => n.id).join(','),
  () => {
    // 换了另一张图（重新建图/换检索范围）：展开态、隐藏社区、手动视图都失效了
    expanded.value = new Set()
    hidden.value = new Set()
    roamed = false
  },
)

onBeforeUnmount(() => {
  if (fitTimer) clearTimeout(fitTimer)
  fitTimer = null
  resizeObserver?.disconnect()
  resizeObserver = null
  chart?.dispose()
  chart = null
  chartReady.value = false
})

// 供父级（面板/自检）复用：重置视图、按内容自适应缩放、读出当前矢量快照
defineExpose({ fit, resetView, fitView, viewScale, svgString, exportPng, exportSvg })
</script>

<template>
  <div class="relative h-full w-full">
    <div ref="el" class="h-full w-full" />

    <!-- 工具栏：缩放 / 重置 / 导出 -->
    <div
      class="absolute top-2.5 right-2.5 flex items-center gap-1 rounded-lg border border-hairline bg-surface/95 px-1.5 py-1 shadow-sm backdrop-blur-sm"
    >
      <button type="button" :class="iconBtnCls('sm')" title="放大" aria-label="放大" @click="zoomBy(1.25)">
        <PhMagnifyingGlassPlus :size="13" />
      </button>
      <button type="button" :class="iconBtnCls('sm')" title="缩小" aria-label="缩小" @click="zoomBy(0.8)">
        <PhMagnifyingGlassMinus :size="13" />
      </button>
      <button
        type="button"
        :class="iconBtnCls('sm')"
        title="重置布局与视图"
        aria-label="重置布局与视图"
        @click="resetView"
      >
        <PhArrowsCounterClockwise :size="13" />
      </button>
      <span class="mx-0.5 h-4 w-px bg-hairline" />
      <button
        type="button"
        :class="iconBtnCls('sm')"
        :disabled="!chartReady"
        title="导出 PNG（2×）"
        aria-label="导出 PNG"
        @click="exportPng"
      >
        <PhImage :size="13" />
      </button>
      <button
        type="button"
        :class="iconBtnCls('sm')"
        :disabled="!chartReady"
        title="导出 SVG（矢量，可再编辑）"
        aria-label="导出 SVG"
        @click="exportSvg"
      >
        <PhFileSvg :size="13" />
      </button>
    </div>

    <!-- 规模与降级提示 -->
    <div
      class="pointer-events-none absolute top-2.5 left-2.5 rounded-md bg-surface/90 px-2 py-1 text-2xs text-ink-4 shadow-xs"
    >
      {{ renderNodes.length }} 节点 · {{ renderEdges.length }} 边
      <span v-if="degraded" class="text-warn">· 已简化渲染</span>
    </div>

    <!-- 社区图例 + 时间轴 -->
    <div class="pointer-events-none absolute inset-x-2 bottom-2 flex flex-col gap-1.5">
      <div
        v-if="communities.length"
        class="pointer-events-auto flex max-h-20 flex-wrap items-center gap-1 overflow-y-auto scroll-slim rounded-lg border border-hairline bg-surface/95 px-2 py-1.5 shadow-sm backdrop-blur-sm"
      >
        <span class="mr-0.5 shrink-0 text-2xs text-ink-4">社区</span>
        <button
          v-for="c in communities"
          :key="c.id"
          type="button"
          class="inline-flex shrink-0 items-center gap-1 rounded-full border border-hairline px-1.5 py-0.5 text-2xs transition-opacity"
          :class="hidden.has(c.id) ? 'opacity-40' : 'opacity-100 hover:bg-hover'"
          :style="{ color: hidden.has(c.id) ? undefined : c.color }"
          :title="hidden.has(c.id) ? '点一下显示这个社区' : '点一下隐藏这个社区'"
          @click="toggleCommunity(c.id)"
        >
          <span class="size-2 shrink-0 rounded-full" :style="{ background: c.color }" />
          <span :class="hidden.has(c.id) ? 'line-through' : ''">#{{ c.id < 0 ? '未分组' : c.id }}</span>
          <span class="text-ink-4">{{ c.count }}</span>
        </button>
      </div>

      <div
        v-if="yearBounds"
        class="pointer-events-auto flex items-center gap-2 rounded-lg border border-hairline bg-surface/95 px-2 py-1.5 shadow-sm backdrop-blur-sm"
      >
        <PhCalendarBlank :size="12" class="shrink-0 text-ink-4" />
        <span class="shrink-0 text-2xs text-ink-4">时间轴</span>
        <input
          class="h-1 min-w-0 flex-1 cursor-pointer appearance-none rounded-full bg-sunken accent-brand"
          type="range"
          :min="yearBounds.min"
          :max="yearBounds.max"
          :value="yearFrom ?? yearBounds.min"
          aria-label="起始年份"
          @input="setYear('from', Number(($event.target as HTMLInputElement).value))"
        />
        <span class="shrink-0 text-2xs tabular-nums text-ink-2">
          {{ yearFrom ?? yearBounds.min }} – {{ yearTo ?? yearBounds.max }}
        </span>
        <input
          class="h-1 min-w-0 flex-1 cursor-pointer appearance-none rounded-full bg-sunken accent-brand"
          type="range"
          :min="yearBounds.min"
          :max="yearBounds.max"
          :value="yearTo ?? yearBounds.max"
          aria-label="结束年份"
          @input="setYear('to', Number(($event.target as HTMLInputElement).value))"
        />
        <button
          v-if="yearFiltered"
          type="button"
          class="shrink-0 text-2xs text-brand hover:underline"
          @click="clearYears"
        >
          全部
        </button>
      </div>
    </div>
  </div>
</template>
