/**
 * 后端契约类型。**手写的**，与 backend/app/schemas 一一对应。
 *
 * 为什么不自动生成：后端 schema 还会持续演进，自动生成的 d.ts 每次都要跟着
 * 改 openapi 的版本与生成器配置；而这里真正被前端消费的字段不到 20 个，
 * 手写一遍更可控，且改动时 TypeScript 会直接把编译错误指到出错的那一行。
 * 后端加字段前端不受影响（多的字段忽略即可）。
 */

// ---------------------------------------------------------------- 统一响应体
export interface ApiResponse<T> {
  code: number
  data: T | null
  message: string
}

export interface PageMeta {
  total: number
  page: number
  page_size: number
  has_next: boolean
}

export interface Page<T> {
  items: T[]
  meta: PageMeta
}

export interface HealthComponent {
  name: string
  ok: boolean
  latency_ms?: number | null
  detail?: string | null
}

export interface HealthReport {
  status: 'ok' | 'degraded' | 'down'
  version: string
  env: string
  components: HealthComponent[]
}

// ---------------------------------------------------------------- 论文
export type PaperStatus = 'pending' | 'parsing' | 'indexing' | 'ready' | 'failed'

export interface Author {
  name: string
  affiliation?: string
}

export interface Paper {
  id: string
  title: string
  authors: Author[]
  abstract?: string | null
  year?: number | null
  venue?: string | null
  doi?: string | null
  arxiv_id?: string | null
  pmid?: string | null
  tags: string[]
  source: string
  status: PaperStatus
  num_chunks: number
  page_count?: number | null
  file_size?: number | null
  /** 实际生效的解析器（mineru / pymupdf / pdfplumber / pypdf）。MinerU 降级时靠它辨认。 */
  parser?: string | null
  error?: string | null
  indexed_at?: string | null
  created_at: string
  updated_at: string
}

export interface PaperDetail extends Paper {
  pdf_path?: string | null
  meta: Record<string, unknown>
}

export interface PaperChunk {
  id: string
  chunk_index: number
  content: string
  section_name?: string | null
  page_start?: number | null
  page_end?: number | null
  token_count: number
}

export interface PaperUploadResult {
  paper: Paper
  /** 后台解析任务 id（进程内执行）；index=false 时为 null */
  task_id?: string | null
}

// ---------------------------------------------------------------- 问答 / 溯源
export interface Citation {
  marker?: number | null
  chunk_id: string
  paper_id: string
  title: string
  section?: string | null
  /** 后端 `CitationOut.page`。 */
  page?: number | null
  /**
   * `page` 的别名。后端历史字段叫 `page`，前端按规格读 `page_start` ——
   * 取值一律用 `pageStart(c)`，两边都认才不会出"页码显示为空"。
   */
  page_start?: number | null
  page_end?: number | null
  /** 归一化定位框：`[x0,y0,x1,y1]` 或 `{page, boxes:[[...]]}`（见 Chunk.bbox）。 */
  bbox?: unknown
  quote: string
  /** 答案里被这条引用支撑的那句话。 */
  answer_span?: string
  nli_score: number
  confidence?: number
  verified: boolean
  attribution_method?: 'self_citation' | 'nli' | 'hybrid'
}

/** 引用在哪一页。`page` 与 `page_start` 谁有值用谁。 */
export function pageStart(c: Citation): number | null {
  // 0 按"没有"处理：页码是 1-based，而后端流式帧里 `page_start or 0` 会把缺省值写成 0。
  // 不挡掉它，点引用就会跳到第 0 页（等于不跳）。
  return c.page || c.page_start || null
}

/** 后端 `SourceTraceOut` —— 「答案片段 ← 哪块证据」的对应关系。 */
export interface SourceTrace {
  answer_span: string
  chunk_id: number | null
  paper_id: number | null
  page: number | null
  bbox: unknown
  confidence: number
  attribution_method: 'self_citation' | 'nli' | 'hybrid'
}

/** 跨篇对比的时间轴节点（后端 `TimelineItem`）。 */
export interface TimelineItem {
  paper_id: number | null
  title: string
  year: number | null
  arxiv_id: string | null
  chunks: number
}

export interface RetrievedChunk {
  chunk_id: string
  paper_id: string
  title: string
  section?: string | null
  page_start?: number | null
  page_end?: number | null
  score: number
  rerank_score?: number | null
  sources: string[]
  preview: string
}

export interface RetrievalDebug {
  crag_level?: 'relevant' | 'ambiguous' | 'irrelevant' | null
  rewrite_round: number
  rewritten_query: string
  rerank_applied: boolean
  top_k: number
  trace: Record<string, unknown>[]
}

export interface AskResult {
  answer: string
  intent: string
  intent_confidence: number
  citations: Citation[]
  /** 「答案片段 ← 证据」的对应关系，规格契约里的 `sources[]`。 */
  sources: SourceTrace[]
  grounding_ratio: number
  /** Reflector 的忠实度评分；无评审时为 null。 */
  faithfulness?: number | null
  passed_grounding: boolean
  unsupported_claims: string[]
  guardrail_flags: string[]
  plan: PlanStep[]
  reflections: Reflection[]
  /** 跨篇对比的方法演进时间轴（按年份升序）。 */
  timeline: TimelineItem[]
  retrieved: RetrievedChunk[]
  debug?: RetrievalDebug | null
  usage: Record<string, number>
  latency_ms: number
  history_id?: number | null
}

export interface TraceResult {
  grounding_ratio: number
  sentences_total: number
  sentences_supported: number
  passed: boolean
  phantom_markers: number[]
  unsupported_claims: string[]
  uncited_claims: string[]
  number_mismatches: string[]
  citations: Citation[]
}

// ---------------------------------------------------------------- Agent 过程
export type IntentKind =
  | 'single_paper_qa'
  | 'cross_paper_reasoning'
  | 'literature_search'
  | 'graph_analysis'
  | 'writing_assist'
  | 'visualization'
  | 'translation'
  | 'chitchat'

export interface PlanStep {
  step?: number
  goal?: string
  tool?: string
  args?: Record<string, unknown>
  result?: string
  status?: string
}

export interface Reflection {
  round?: number
  scores?: Partial<Record<'faithfulness' | 'relevance' | 'coherence' | 'completeness', number>>
  overall?: number
  verdict?: string
  critique?: string
}

// ---------------------------------------------------------------- 引文图谱
export interface GraphNode {
  id: string
  title: string
  year?: number | null
  in_degree: number
  out_degree: number
  community?: number | null
  pagerank?: number | null
}

export interface GraphEdge {
  source: string
  target: string
  weight: number
}

export interface GraphOut {
  nodes: GraphNode[]
  edges: GraphEdge[]
  n_nodes: number
  n_edges: number
  truncated: boolean
}

export type AnalysisKind = 'overview' | 'pagerank' | 'communities' | 'centrality' | 'paths' | 'timeline'

export interface GraphAnalysisResult {
  analysis: string
  n_nodes: number
  n_edges: number
  result: unknown
  note: string
}

// ---------------------------------------------------------------- 写作
export interface WritingTemplate {
  kind: string
  name: string
  description: string
  default_length: number
  outline: string[]
}

export interface WriteResult {
  kind: string
  content: string
  citations: Record<string, unknown>[]
  grounding_ratio: number
  retrieved_count: number
  usage: Record<string, number>
}

export interface BilingualPair {
  source: string
  target: string
}

export interface TranslateResult {
  text: string
  pairs: BilingualPair[]
  glossary: Record<string, string>[]
  usage: Record<string, number>
}

// ---------------------------------------------------------------- 工具
export interface ToolInfo {
  name: string
  description: string
  kind: 'remote' | 'local' | string
  server: string
  args_schema: Record<string, unknown>
}

export interface ToolListOut {
  tools: ToolInfo[]
  enabled_servers: string[]
  note: string
}

export interface ToolCallResult {
  name: string
  ok: boolean
  result: unknown
  error?: string | null
  latency_ms: number
}

// ---------------------------------------------------------------- 会话
export interface Conversation {
  id: string
  title: string
  paper_ids: string[]
  message_count: number
  created_at?: string | null
  updated_at?: string | null
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  intent?: string | null
  citations: Citation[]
  grounding_ratio?: number | null
  /** Reflector 的忠实度评分；这一轮没跑评审时为 null。 */
  faithfulness?: number | null
  created_at?: string | null
}

export interface ConversationDetail extends Conversation {
  messages: ChatMessage[]
}

// ---------------------------------------------------------------- SSE 事件
// 与 backend/app/llm/streaming.py 的 Event 枚举严格对应。
// /chat/stream 用细粒度那一套（intent/plan/tool/reflection/...）；
// /qa/stream 用归并后的 thinking/retrieval/citation/source/token/done。
export type StreamEventName =
  | 'intent'
  | 'clarify'
  | 'plan'
  | 'tool'
  | 'token'
  | 'citation'
  | 'source'
  | 'thinking'
  | 'retrieval'
  | 'reflection'
  | 'replan'
  | 'guardrail'
  | 'error'
  | 'done'
  // ---- /tools/explore 探索闭环（见 backend/app/agents/explore.py 模块头）----
  | 'thought'
  | 'action'
  | 'observation'
  | 'progress'
  | 'recommend'

/** `/qa/stream` 的 thinking 帧（stage 决定前端归到哪一类轨迹）。 */
export interface ThinkingEvent {
  stage: 'intent' | 'clarify' | 'plan' | 'replan' | 'reflection' | string
  intent?: string
  confidence?: number
  question?: string
  steps?: PlanStep[]
  decision?: string
  scores?: Record<string, number>
  overall?: number
  verdict?: string
}

/** `/qa/stream` 的 source 帧 —— 渲染 [paper_id:page:chunk_id] 徽章与 PDF 跳转所需的一切。 */
export interface SourceEvent {
  paper_id: number | null
  page: number | null
  chunk_id: number | null
  bbox: unknown
  title: string
  quote: string
  confidence: number
}

export interface StreamEvent<T = unknown> {
  event: StreamEventName
  data: T
}

// ---------------------------------------------------------------- 主题探索闭环
// 与 backend/app/schemas/explore.py 对应。事件名见 explore.py 的模块头。
export interface ExploreThought {
  stage: string
  text: string
  round: number
}

export interface ExploreAction {
  name: string
  args: Record<string, unknown>
  round: number
}

export interface ExploreObservation {
  name: string
  ok: boolean
  text: string
  /** 其余都是各步骤自带的附加字段（n / new / score / paper_id / verdict…）。 */
  [key: string]: unknown
}

export interface ExploreProgress {
  stage: string
  /** 1-based，分母固定为 6（STAGES）。 */
  index: number
  total: number
  round: number
  detail: string
}

export interface Recommendation {
  arxiv_id: string | null
  paper_id: number | null
  title: string
  authors: string[]
  year: number | null
  venue: string
  url: string
  source: string
  /** 与主题的 bge-m3 余弦相似度，0~1。 */
  score: number
  citation_count: number
  abstract: string
  downloaded: boolean
  note: string
}

export interface ExploreDone {
  topic: string
  rounds: number
  searched: number
  scored: number
  downloaded: number
  /** 其中本次新入库的篇数。 */
  new: number
  passed: number
  min_score: number
  quality: number
  verdict: string
  critique: string
  recommendations: Recommendation[]
  elapsed_ms: number
}

// ---------------------------------------------------------------- 订阅 / 调度
export interface Subscription {
  id: number
  topic: string
  max_papers: number
  min_score: number
  enabled: boolean
  last_run_at: string | null
  /** pending | running | ok | failed */
  last_status: string
  last_error: string | null
  /** 上一次抓取新入库的篇数。 */
  last_new: number
  total_new: number
  created_at: string | null
}

export interface SchedulerStatus {
  enabled: boolean
  hour: number
  minute: number
  next_run_at: string | null
  /** 实现方式说明（本项目是 in-process asyncio）。 */
  engine: string
}
