import type {
  ChatMessage,
  Citation,
  Conversation,
  ConversationDetail,
  IntentKind,
  PlanStep,
  Reflection,
  ThinkingEvent,
  TimelineItem,
} from '~/types/api'
import type { ReaderSelection } from '~/types/workbench'

export interface ToolTraceItem {
  name: string
  status: string
  detail?: string
}

/** 挂在输入框上的"引文上下文"。由阅读器划词产生，发送时明确拼进问句。 */
export interface PendingQuote {
  paperId: string
  paperTitle: string
  page: number
  text: string
}

/** 意图 → 中文短名。列表面板与状态栏都要用，所以放 store 里只写一遍。 */
export const INTENT_LABEL: Record<IntentKind, string> = {
  single_paper_qa: '单篇问答',
  cross_paper_reasoning: '跨篇推理',
  literature_search: '文献检索',
  graph_analysis: '图谱分析',
  writing_assist: '写作辅助',
  visualization: '可视化',
  translation: '翻译',
  chitchat: '闲聊',
}

/**
 * 对话状态机。
 *
 * 一个回合的生命周期：`sending = true` → 收 SSE 事件逐项填 live 状态 →
 * `done`/`error` 收尾。live 状态（intent/plan/tools/citations/reflections）
 * 在回合结束后**固化到最后一条 assistant 消息上**，这样回看历史还能看到
 * "当时是怎么查的"，而不是只剩一段结论。
 */
export const useChatStore = defineStore('chat', () => {
  // ---------------- 会话列表 ----------------
  const conversations = ref<Conversation[]>([])
  const activeId = ref<string | null>(null)
  const loadingConversations = ref(false)

  // ---------------- 当前会话 ----------------
  const messages = ref<ChatMessage[]>([])
  const loadingMessages = ref(false)

  // ---------------- 一个回合的实时状态 ----------------
  const sending = ref(false)
  const errorMessage = ref('')
  const clarifyQuestion = ref('')
  const intent = ref<{ intent: IntentKind; confidence: number } | null>(null)
  const plan = ref<PlanStep[]>([])
  const tools = ref<ToolTraceItem[]>([])
  const citations = ref<Citation[]>([])
  const reflections = ref<Reflection[]>([])
  const guardrails = ref<{ action: string; flags: string[] }[]>([])
  const replanNote = ref('')
  const groundingRatio = ref<number | null>(null)
  const usage = ref<Record<string, number>>({})
  const latencyMs = ref<number | null>(null)
  /** 跨篇对比的方法演进时间轴。单篇问答后端给空表，这里也就没有内容可画。 */
  const timeline = ref<TimelineItem[]>([])

  /** 本轮回答正文。单独拎出来是为了让 token 追加只改一个字符串，避免整表重渲染。 */
  const answer = ref('')

  /** 输入框上的引文上下文。 */
  const quote = ref<PendingQuote | null>(null)

  const stream = useChatStream()

  const hasTrace = computed(
    () =>
      Boolean(intent.value) ||
      plan.value.length > 0 ||
      tools.value.length > 0 ||
      reflections.value.length > 0 ||
      guardrails.value.length > 0,
  )

  function resetTurn() {
    errorMessage.value = ''
    clarifyQuestion.value = ''
    intent.value = null
    plan.value = []
    tools.value = []
    citations.value = []
    reflections.value = []
    guardrails.value = []
    replanNote.value = ''
    groundingRatio.value = null
    usage.value = {}
    latencyMs.value = null
    timeline.value = []
    answer.value = ''
  }

  function pushTool(item: ToolTraceItem) {
    const last = tools.value.at(-1)
    // 同名工具的 running → done 合并成一条，不然时间线会刷屏
    if (last && last.name === item.name && last.status === 'running' && item.status !== 'running') {
      last.status = item.status
      return
    }
    tools.value.push(item)
  }

  // ---------------------------------------------------------------- 引文上下文

  function attachQuote(sel: ReaderSelection, paperTitle: string) {
    quote.value = {
      paperId: sel.paperId,
      paperTitle,
      page: sel.page,
      text: sel.text,
    }
  }

  function clearQuote() {
    quote.value = null
  }

  // ---------------------------------------------------------------- 会话 CRUD

  async function loadConversations() {
    const api = useApi()
    loadingConversations.value = true
    try {
      const res = await api.get<{ items: Conversation[] }>('/chat/conversations', {
        page: 1,
        page_size: 50,
      })
      conversations.value = res?.items ?? []
    } catch (err) {
      errorMessage.value = (err as Error).message
    } finally {
      loadingConversations.value = false
    }
  }

  async function openConversation(id: string) {
    abortStream()
    loadingMessages.value = true
    try {
      const detail = await useApi().get<ConversationDetail>(`/chat/conversations/${id}`)
      activeId.value = detail.id
      messages.value = detail.messages ?? []
      resetTurn()
    } catch (err) {
      errorMessage.value = (err as Error).message
    } finally {
      loadingMessages.value = false
    }
  }

  function newConversation() {
    abortStream()
    activeId.value = null
    messages.value = []
    resetTurn()
    clearQuote()
  }

  async function removeConversation(id: string) {
    await useApi().del<Record<string, unknown>>(`/chat/conversations/${id}`)
    conversations.value = conversations.value.filter((c) => c.id !== id)
    if (activeId.value === id) newConversation()
  }

  // ---------------------------------------------------------------- 发问

  /**
   * 发一问。
   *
   * 引文上下文不是偷偷塞进请求体的：它被**明文拼进问句**，用户在输入框里
   * 能看到自己到底问的是什么。后端 ChatRequest 只有 query/conversation_id/
   * paper_ids/intent 四个字段，硬加一个 quote 字段就得同时改 schema、图状态与
   * prompt —— 为了一个前端展示问题不值得。
   */
  async function send(text: string, paperIds: string[] = []) {
    const body = text.trim()
    if (!body || sending.value) return

    resetTurn()
    sending.value = true

    const q = quote.value
    const composed = q
      ? `以下是我在《${q.paperTitle}》第 ${q.page} 页选中的原文：\n\n> ${q.text.replace(/\n/g, '\n> ')}\n\n${body}`
      : body

    messages.value.push({
      id: `local-user-${Date.now()}`,
      role: 'user',
      content: composed,
      citations: [],
    })
    clearQuote()

    // 附带引文时把范围收到那一篇：选了原文却去全库检索，答非所问的概率很高
    const scope = q ? [q.paperId] : paperIds

    await stream.start(
      { query: composed, conversation_id: activeId.value, paper_ids: scope },
      {
        onEvent: (evt) => {
          switch (evt.event) {
            case 'intent': {
              const d = evt.data as { intent: IntentKind; confidence: number }
              intent.value = { intent: d.intent, confidence: d.confidence }
              break
            }
            case 'clarify':
              clarifyQuestion.value = (evt.data as { question: string }).question || ''
              break
            case 'plan':
              plan.value = ((evt.data as { steps: PlanStep[] }).steps || []).slice()
              break
            case 'tool': {
              const d = evt.data as { name: string; status: string; crag_level?: string; n?: number }
              if (d?.name) {
                pushTool({
                  name: d.name,
                  status: d.status || 'done',
                  detail: d.crag_level ? `CRAG=${d.crag_level} · 命中 ${d.n ?? 0} 块` : undefined,
                })
              }
              break
            }
            case 'token':
              answer.value += (evt.data as { text: string }).text || ''
              break
            case 'citation': {
              const cite = evt.data as Citation
              const dup = citations.value.some((c) => c.marker === cite.marker && c.chunk_id === cite.chunk_id)
              if (!dup) citations.value.push(cite)
              break
            }
            case 'thinking': {
              // /qa/stream 把 intent/plan/reflection/replan 归并成 thinking（带 stage）
              const d = evt.data as ThinkingEvent
              if (d.stage === 'plan') plan.value = (d.steps ?? []).slice()
              else if (d.stage === 'clarify') clarifyQuestion.value = d.question || ''
              else if (d.stage === 'intent' && d.intent) {
                intent.value = { intent: d.intent as IntentKind, confidence: d.confidence ?? 0 }
              } else if (d.stage === 'replan') replanNote.value = d.decision || ''
              else if (d.stage === 'reflection' && d.scores) {
                reflections.value.push({
                  round: reflections.value.length + 1,
                  scores: d.scores as Reflection['scores'],
                  overall: d.overall,
                  verdict: d.verdict,
                })
              }
              break
            }
            case 'retrieval': {
              const d = evt.data as { name: string; status: string; n?: number; crag_level?: string }
              if (d?.name) {
                pushTool({
                  name: d.name,
                  status: d.status || 'done',
                  detail: d.crag_level ? `CRAG=${d.crag_level} · 命中 ${d.n ?? 0} 块` : undefined,
                })
              }
              break
            }
            case 'source':
              // source 帧与 citation 帧同源（都是同一条引用），字段已被上面的 citation 收下，
              // 这里刻意不重复收集 —— 两个真相源迟早会不一致。
              break
            case 'reflection':
              reflections.value.push(evt.data as Reflection)
              break
            case 'replan':
              replanNote.value = (evt.data as { decision: string }).decision || ''
              break
            case 'guardrail': {
              const d = evt.data as { action: string; flags: string[] }
              guardrails.value.push({ action: d.action || '', flags: d.flags || [] })
              break
            }
            case 'done': {
              const d = evt.data as {
                grounding_ratio?: number
                usage?: Record<string, number>
                latency_ms?: number
                timeline?: TimelineItem[]
              }
              groundingRatio.value = d.grounding_ratio ?? null
              usage.value = d.usage ?? {}
              latencyMs.value = d.latency_ms ?? null
              timeline.value = d.timeline ?? []
              break
            }
            case 'error': {
              const d = evt.data as { code: number; message: string }
              errorMessage.value = `${d.message}（code ${d.code}）`
              break
            }
          }
        },
        onError: (err) => {
          errorMessage.value = err.message
          answer.value ||= '（本次回答失败，未产生内容）'
        },
        onClose: () => {
          messages.value.push({
            id: `local-assistant-${Date.now()}`,
            role: 'assistant',
            content: answer.value,
            intent: intent.value?.intent ?? null,
            citations: citations.value.slice(),
            grounding_ratio: groundingRatio.value,
            // 忠实度取自最后一次评审的分数：done 帧里没带它，而评审可能一次都没跑
            faithfulness: reflections.value.at(-1)?.scores?.faithfulness ?? null,
          })
          sending.value = false
          void loadConversations()
        },
      },
    )
  }

  function abortStream() {
    stream.abort()
    sending.value = false
  }

  // 引用编号 → 该条引用（给角标点击跳转用）
  const citationByMarker = computed(() => {
    const map = new Map<number, Citation>()
    for (const c of citations.value) if (c.marker != null) map.set(c.marker, c)
    return map
  })

  const verifiedMarkers = computed(
    () => new Set(citations.value.filter((c) => c.verified && c.marker != null).map((c) => c.marker!)),
  )

  return {
    conversations,
    activeId,
    loadingConversations,
    loadConversations,
    openConversation,
    newConversation,
    removeConversation,

    messages,
    loadingMessages,

    sending,
    errorMessage,
    clarifyQuestion,
    intent,
    plan,
    tools,
    citations,
    reflections,
    guardrails,
    replanNote,
    groundingRatio,
    usage,
    latencyMs,
    timeline,
    answer,
    hasTrace,
    citationByMarker,
    verifiedMarkers,

    quote,
    attachQuote,
    clearQuote,

    send,
    abortStream,
    resetTurn,
  }
})
