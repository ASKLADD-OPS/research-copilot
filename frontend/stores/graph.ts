import type {
  AnalysisKind,
  FutureIdea,
  GraphAnalysisResult,
  GraphBuildResult,
  GraphEdge,
  GraphInsightResult,
  GraphNode,
  GraphOut,
  Keystone,
} from '~/types/api'
import type { GraphConfig } from '~/types/workbench'

export const DEFAULT_GRAPH_CONFIG: GraphConfig = {
  layout: 'force',
  repulsion: 420,
  edgeLength: [40, 150],
  gravity: 0.08,
  showLabels: true,
  labelMaxNodes: 40,
  colorBy: 'community',
  minDegree: 0,
  nodeScale: 1,
  edgeOpacity: 0.7,
  highlightNeighbors: true,
}

const CONFIG_KEY = 'rc-graph-config'

/**
 * 引文图谱。
 *
 * 分成两半：`raw` 是后端给的完整图（一成不变），`config` 是"怎么画"。
 * 过滤与着色都在前端算 —— 后端裁剪到 300 节点是性能护栏，不是"数据已经削过了"，
 * 用户想按度数再看一刀是常态，为这个再打一次接口不值。
 *
 * 年份区间**不放进 config**：config 是"长相"（会存本机、下次打开还在），
 * 年份是"这次想看哪一段"的查询条件，混在一起会让用户下次打开时莫名其妙少一半节点。
 */
export const useGraphStore = defineStore('graph', () => {
  const nodes = ref<GraphNode[]>([])
  const edges = ref<GraphEdge[]>([])
  const truncated = ref(false)
  const loading = ref(false)
  const loaded = ref(false)
  const errorMessage = ref('')

  const config = ref<GraphConfig>({ ...DEFAULT_GRAPH_CONFIG })

  /** 时间轴过滤。null = 不设限。 */
  const yearFrom = ref<number | null>(null)
  const yearTo = ref<number | null>(null)

  const selectedNodeId = ref<string | null>(null)

  const analysisKind = ref<AnalysisKind>('overview')
  const analysis = ref<GraphAnalysisResult | null>(null)
  const analyzing = ref(false)

  // ---- 建图 / 综述的产出 ----
  const snapshotId = ref<number | null>(null)
  const keystones = ref<Keystone[]>([])
  const mainline = ref<GraphBuildResult['mainline']>([])
  const timeline = ref<GraphBuildResult['timeline']>([])
  const building = ref(false)

  const insight = ref<GraphInsightResult | null>(null)
  const futureIdeas = ref<FutureIdea[]>([])
  const droppedIdeas = ref<{ idea: string; reason: string }[]>([])
  const insighting = ref(false)

  // ---------------------------------------------------------------- 派生

  /** 度数 = 入度 + 出度。筛选与着色都用它，"被引数"单独看会漏掉综述类节点。 */
  function degree(n: GraphNode) {
    return n.degree ?? n.in_degree + n.out_degree
  }

  /** 图里出现过的年份区间 —— 时间轴滑杆的范围就取它。 */
  const yearBounds = computed(() => {
    const years = nodes.value.map((n) => n.year).filter((y): y is number => typeof y === 'number')
    if (!years.length) return null
    return { min: Math.min(...years), max: Math.max(...years) }
  })

  function inYearRange(n: GraphNode) {
    if (typeof n.year !== 'number') return yearFrom.value === null && yearTo.value === null
    if (yearFrom.value !== null && n.year < yearFrom.value) return false
    if (yearTo.value !== null && n.year > yearTo.value) return false
    return true
  }

  /** 过了 minDegree 与年份两道刀、真正会画出来的节点。 */
  const visibleNodes = computed(() =>
    nodes.value.filter((n) => degree(n) >= config.value.minDegree && inYearRange(n)),
  )

  const visibleNodeIds = computed(() => new Set(visibleNodes.value.map((n) => n.id)))

  /** 两端都还活着的边。只过滤节点不过滤边，会留下指向空气的箭头。 */
  const visibleEdges = computed(() =>
    edges.value.filter((e) => visibleNodeIds.value.has(e.source) && visibleNodeIds.value.has(e.target)),
  )

  const maxInDegree = computed(() => Math.max(1, ...visibleNodes.value.map((n) => n.in_degree)))
  const maxPageRank = computed(() => Math.max(1e-6, ...visibleNodes.value.map((n) => n.pagerank ?? 0)))
  const maxDegree = computed(() => Math.max(1, ...visibleNodes.value.map(degree)))

  const selectedNode = computed(() => nodes.value.find((n) => n.id === selectedNodeId.value) ?? null)

  /** 选中节点的直接邻居 + 自身。开启邻居高亮时用这个集合压暗其余节点。 */
  const highlighted = computed(() => {
    const id = selectedNodeId.value
    if (!id || !config.value.highlightNeighbors) return null
    const set = new Set<string>([id])
    for (const e of visibleEdges.value) {
      if (e.source === id) set.add(e.target)
      if (e.target === id) set.add(e.source)
    }
    return set
  })

  /** 孤立点数量 —— 提示用户"图上有一半点没连线"比让他自己数强。 */
  const isolatedCount = computed(() => visibleNodes.value.filter((n) => degree(n) === 0).length)

  const isEmpty = computed(() => !loading.value && loaded.value && visibleNodes.value.length === 0)

  /** 每个节点连出去的边（按 id 归并）—— 点击展开邻居、hover 看关系都要用。 */
  const adjacency = computed(() => {
    const map = new Map<string, { out: GraphEdge[]; in: GraphEdge[] }>()
    const slot = (id: string) => {
      let hit = map.get(id)
      if (!hit) {
        hit = { out: [], in: [] }
        map.set(id, hit)
      }
      return hit
    }
    for (const e of edges.value) {
      slot(e.source).out.push(e)
      slot(e.target).in.push(e)
    }
    return map
  })

  /** 点一个节点就把它的邻居拉进"已展开"集合 —— 图太大时逐层看比一次摊开清楚。 */
  const expanded = ref<Set<string>>(new Set())

  function neighborsOf(id: string): string[] {
    const slot = adjacency.value.get(id)
    if (!slot) return []
    return [...slot.out.map((e) => e.target), ...slot.in.map((e) => e.source)]
  }

  // ---------------------------------------------------------------- 读

  function applyGraph(res: GraphOut) {
    nodes.value = res.nodes
    edges.value = res.edges
    truncated.value = res.truncated
    loaded.value = true
    expanded.value = new Set()
    // 选中的节点可能被重新裁剪掉了
    if (selectedNodeId.value && !res.nodes.some((n) => n.id === selectedNodeId.value)) {
      selectedNodeId.value = null
    }
  }

  async function load(opts: { limit?: number } = {}) {
    loading.value = true
    errorMessage.value = ''
    try {
      applyGraph(await useApi().get<GraphOut>('/graph', { limit: opts.limit }))
    } catch (err) {
      errorMessage.value = (err as Error).message
    } finally {
      loading.value = false
    }
  }

  async function rebuild() {
    loading.value = true
    errorMessage.value = ''
    try {
      await useApi().post<Record<string, unknown>>('/graph/rebuild')
      await load()
    } catch (err) {
      errorMessage.value = (err as Error).message
    } finally {
      loading.value = false
    }
  }

  /** 建图 + 图论分析 + 落快照。`enrich` 会去打 Semantic Scholar，默认关。 */
  async function build(opts: { paperIds?: string[]; enrich?: boolean; limit?: number } = {}) {
    building.value = true
    errorMessage.value = ''
    try {
      const res = await useApi().post<GraphBuildResult>('/graph/build', {
        paper_ids: (opts.paperIds ?? []).map(Number).filter((n) => Number.isFinite(n)),
        limit: opts.limit ?? 20,
        enrich: opts.enrich ?? false,
      })
      snapshotId.value = res.snapshot_id || null
      keystones.value = res.keystones
      mainline.value = res.mainline
      timeline.value = res.timeline
      if (res.graph?.nodes?.length) applyGraph(res.graph)
      if (res.note) errorMessage.value = res.note
      return res
    } catch (err) {
      errorMessage.value = (err as Error).message
      return null
    } finally {
      building.value = false
    }
  }

  /** 领域综述 + 未来方向。`resolvedIdeas` 是"已经做过的方向"，会被用来过滤。 */
  async function insights(opts: { paperIds?: string[]; resolvedIdeas?: string[]; withFuture?: boolean } = {}) {
    insighting.value = true
    errorMessage.value = ''
    try {
      const res = await useApi().post<GraphInsightResult>('/graph/insights', {
        snapshot_id: snapshotId.value,
        paper_ids: (opts.paperIds ?? []).map(Number).filter((n) => Number.isFinite(n)),
        resolved_ideas: opts.resolvedIdeas ?? [],
        with_future: opts.withFuture ?? true,
      })
      insight.value = res
      futureIdeas.value = res.future_ideas
      droppedIdeas.value = res.dropped_ideas
      if (res.note) errorMessage.value = res.note
      return res
    } catch (err) {
      errorMessage.value = (err as Error).message
      insight.value = null
      return null
    } finally {
      insighting.value = false
    }
  }

  async function analyze(kind: AnalysisKind = analysisKind.value, paperIds: string[] = []) {
    analyzing.value = true
    analysisKind.value = kind
    errorMessage.value = ''
    try {
      analysis.value = await useApi().post<GraphAnalysisResult>('/graph/analyze', {
        analysis: kind,
        paper_ids: paperIds,
      })
    } catch (err) {
      errorMessage.value = (err as Error).message
      analysis.value = null
    } finally {
      analyzing.value = false
    }
  }

  // ---------------------------------------------------------------- 选中

  function selectNode(id: string | null) {
    selectedNodeId.value = id
  }

  function toggleNode(id: string) {
    selectedNodeId.value = selectedNodeId.value === id ? null : id
  }

  /** 展开一个节点的邻居（把它自己和邻居都标进"已展开"）。 */
  function expandNode(id: string) {
    const next = new Set(expanded.value)
    next.add(id)
    for (const n of neighborsOf(id)) next.add(n)
    expanded.value = next
  }

  function setYearRange(from: number | null, to: number | null) {
    yearFrom.value = from
    yearTo.value = to
  }

  function resetYearRange() {
    yearFrom.value = null
    yearTo.value = null
  }

  // ---------------------------------------------------------------- 配置

  function patchConfig(patch: Partial<GraphConfig>) {
    config.value = { ...config.value, ...patch }
  }

  function resetConfig() {
    config.value = { ...DEFAULT_GRAPH_CONFIG }
  }

  function restoreConfig() {
    if (!import.meta.client) return
    try {
      const raw = localStorage.getItem(CONFIG_KEY)
      if (!raw) return
      // 只认已知键，避免旧版本留在 localStorage 里的垃圾字段污染 config。
      // 这里的双重断言不是偷懒：key 是 keyof GraphConfig 的联合类型，TS 5 对
      // "联合键写联合值"会判成不可赋值（相关性联合），必须在 unknown 上中转一次。
      const saved = JSON.parse(raw) as Partial<GraphConfig>
      const next: GraphConfig = { ...DEFAULT_GRAPH_CONFIG }
      const writable = next as unknown as Record<string, unknown>
      for (const key of Object.keys(DEFAULT_GRAPH_CONFIG) as (keyof GraphConfig)[]) {
        if (saved[key] !== undefined) writable[key] = saved[key]
      }
      config.value = next
    } catch {
      /* localStorage 坏了就当没存过 */
    }
  }

  if (import.meta.client) {
    watch(
      config,
      (v) => {
        try {
          localStorage.setItem(CONFIG_KEY, JSON.stringify(v))
        } catch {
          /* 隐私模式禁写，忽略 */
        }
      },
      { deep: true },
    )
  }

  return {
    nodes,
    edges,
    truncated,
    loading,
    loaded,
    errorMessage,
    config,
    yearFrom,
    yearTo,
    yearBounds,
    snapshotId,
    keystones,
    mainline,
    timeline,
    building,
    insight,
    futureIdeas,
    droppedIdeas,
    insighting,
    selectedNodeId,
    selectedNode,
    analysisKind,
    analysis,
    analyzing,
    visibleNodes,
    visibleEdges,
    visibleNodeIds,
    highlighted,
    adjacency,
    expanded,
    maxInDegree,
    maxPageRank,
    maxDegree,
    isolatedCount,
    isEmpty,
    degree,
    neighborsOf,
    load,
    rebuild,
    build,
    insights,
    analyze,
    selectNode,
    toggleNode,
    expandNode,
    setYearRange,
    resetYearRange,
    patchConfig,
    resetConfig,
    restoreConfig,
  }
})
