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
  page_start?: number | null
  page_end?: number | null
  quote: string
  nli_score: number
  verified: boolean
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
  grounding_ratio: number
  passed_grounding: boolean
  unsupported_claims: string[]
  guardrail_flags: string[]
  plan: PlanStep[]
  reflections: Reflection[]
  retrieved: RetrievedChunk[]
  debug?: RetrievalDebug | null
  usage: Record<string, number>
  latency_ms: number
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
  created_at?: string | null
}

export interface ConversationDetail extends Conversation {
  messages: ChatMessage[]
}

// ---------------------------------------------------------------- SSE 事件
// 与 backend/app/llm/streaming.py 的 Event 枚举严格对应
export type StreamEventName =
  | 'intent'
  | 'clarify'
  | 'plan'
  | 'tool'
  | 'token'
  | 'citation'
  | 'reflection'
  | 'replan'
  | 'guardrail'
  | 'error'
  | 'done'

export interface StreamEvent<T = unknown> {
  event: StreamEventName
  data: T
}
