/**
 * 工作台自身的类型（与后端无关的那一半）。
 *
 * 这里只放"前端独有的形态"：面板布局、阅读锚点、图谱配置。
 * 凡是后端返回的东西一律在 `api.ts`，两边不要互相 import —— 后端契约会独立演进。
 */

// ---------------------------------------------------------------- 面板与布局

/** 工作台的三个可折叠面板。rail 是固定的图标轨，不参与拖拽，所以不在这个联合里。 */
export type PanelKey = 'library' | 'main' | 'work'

/** 中央主视图：同一块地方切换阅读器与引文图谱。 */
export type MainView = 'reader' | 'graph'

/** 右栏的标签页。写作与工具也搬进来，这样"不切工具"这件事才成立。 */
export type WorkTab = 'chat' | 'inspect' | 'writing' | 'tools'

export type PanelSide = 'left' | 'right'

export interface PanelLayout {
  /** 面板宽度（px）。折叠时**保留**最后的值 —— 展开要还原成原样，不是回默认值。 */
  size: number
  collapsed: boolean
}

/** 三栏布局的完整状态。这个对象整体进 localStorage。 */
export interface WorkbenchLayout {
  mainView: MainView
  workTab: WorkTab
  library: PanelLayout
  work: PanelLayout
}

// ---------------------------------------------------------------- 阅读器锚点

/** 归一化矩形：x/y/w/h 都是 [0,1] 的比例值，相对页面宽高。 */
export interface NormRect {
  x: number
  y: number
  w: number
  h: number
}

/**
 * 一次划词。
 *
 * 坐标存**归一化**值而不是像素：用户随时会缩放，存像素的话缩放后高亮框就飘了。
 */
export interface ReaderSelection {
  paperId: string
  /** 1-based */
  page: number
  text: string
  rects: NormRect[]
  /** 毫秒时间戳，用来按时间倒序排"划词记录" */
  createdAt: number
}

/**
 * 跳转指令：引用角标 / 溯源 / 图谱节点 → 阅读器。
 *
 * `nonce` 是关键：同一条引用被点两次时，page 与 quote 都没变，
 * 光靠 watch 前两个字段不会触发第二次滚动。每次都换一个 nonce 才能保证"点了就跳"。
 */
export interface ReaderTarget {
  paperId: string
  /** 1-based */
  page: number
  quote?: string
  nonce: number
}

// ---------------------------------------------------------------- 图谱配置

export type GraphLayout = 'force' | 'circular' | 'none'

/** 节点着色维度。research 场景真正有意义的就这三个。 */
export type GraphColorBy = 'community' | 'year' | 'degree'

export interface GraphConfig {
  layout: GraphLayout
  /** 斥力因子，越大节点越散 */
  repulsion: number
  /** 边长区间 [min, max] */
  edgeLength: [number, number]
  /** 向心力，越大越向中心收拢 */
  gravity: number
  /** 节点总数 ≤ labelMaxNodes 时才默认显示标签 */
  showLabels: boolean
  labelMaxNodes: number
  colorBy: GraphColorBy
  /** 度数低于此值的节点直接不画 —— 引文图里大量孤立点是纯噪音 */
  minDegree: number
  nodeScale: number
  edgeOpacity: number
  highlightNeighbors: boolean
}

// 默认值放在 store 里（见 stores/graph.ts 的 DEFAULT_GRAPH_CONFIG）：
// types/ 只放类型，不放运行时会用到的值，免得工具链把它当成"有副作用"的模块。

// ---------------------------------------------------------------- 后台任务

export type TaskKind = 'index_paper' | 'rebuild_edges' | string
export type TaskStatus = 'pending' | 'running' | 'done' | 'failed'

/** 与 backend/app/schemas/task.py::TaskOut 对齐。 */
export interface TaskOut {
  id: string
  kind: TaskKind
  status: TaskStatus
  progress: number
  payload: Record<string, unknown>
  result: Record<string, unknown> | null
  error: string | null
  attempts: number
  started_at: string | null
  finished_at: string | null
}

/** 提交给搜索引擎的检索范围。 */
export interface RetrievalScope {
  paperIds: string[]
  /** 只在某一页附近检索（划词追问时用） */
  page?: number | null
  topK: number
}
