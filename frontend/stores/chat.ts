import type {
  ChatMessage,
  Citation,
  Conversation,
  ConversationDetail,
  PlanStep,
  Reflection,
} from '~/types/api'

export interface ToolTraceItem {
  name: string
  status: string
  detail?: string
}

/**
 * 对话状态机。
 *
 * 一个回合的生命周期：`sending = true` → 收 SSE 事件逐项填 live 状态 →
 * `done`/`error` 收尾。live 状态（intent/plan/tools/citations/reflections）
 * 在回合结束后**保留**在最后一条 assistant 消息上，这样回看历史也能看到
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
  const intent = ref<{ intent: string; confidence: number } | null>(null)
  const plan = ref<PlanStep[]>([])
  const tools = ref<ToolTraceItem[]>([])
  const citations = ref<Citation[]>([])
  const reflections = ref<Reflection[]>([])
  const guardrails = ref<{ action: string; flags: string[] }[]>([])
  const replanNote = ref('')
  const groundingRatio = ref<number | null>(null)
  const usage = ref<Record<string, number>>({})
  const latencyMs = ref<number | null>(null)

  const stream = useChatStream()

  /** 本轮回答正文。单独拎出来是为了让 token 追加只改一个字符串，避免整表重渲染。 */
  const answer = ref('')

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

  // ---------------------------------------------------------------- 会话 CRUD
  async function loadConversations() {
    const api = useApi()
    loadingConversations.value = true
    try {
      const page = await api.get<{ items: Conversation[] }>('/chat/conversations', {
        page: 1,
        page_size: 50,
      })
      conversations.value = page?.items ?? []
    } catch (err) {
      errorMessage.value = (err as Error).message
    } finally {
      loadingConversations.value = false
    }
  }

  async function openConversation(id: string) {
    const api = useApi()
    abortStream()
    loadingMessages.value = true
    try {
      const detail = await api.get<ConversationDetail>(`/chat/conversations/${id}`)
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
  }

  async function removeConversation(id: string) {
    const api = useApi()
    await api.del(`/chat/conversations/${id}`)
    conversations.value = conversations.value.filter((c) => c.id !== id)
    if (activeId.value === id) newConversation()
  }

  // ---------------------------------------------------------------- 发问
  async function send(query: string, paperIds: string[] = []) {
    const text = query.trim()
    if (!text || sending.value) return

    resetTurn()
    sending.value = true

    const userMsg: ChatMessage = {
      id: `local-user-${Date.now()}`,
      role: 'user',
      content: text,
      citations: [],
    }
    messages.value.push(userMsg)

    await stream.start(
      { query: text, conversation_id: activeId.value, paper_ids: paperIds },
      {
        onEvent: (evt) => {
          switch (evt.event) {
            case 'intent': {
              const d = evt.data as { intent: string; confidence: number }
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
              const dup = citations.value.some(
                (c) => c.marker === cite.marker && c.chunk_id === cite.chunk_id,
              )
              if (!dup) citations.value.push(cite)
              break
            }
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
              }
              groundingRatio.value = d.grounding_ratio ?? null
              usage.value = d.usage ?? {}
              latencyMs.value = d.latency_ms ?? null
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
          // 把这一轮的实时状态固化到消息上，历史回看时还在
          const md = answer.value
          messages.value.push({
            id: `local-assistant-${Date.now()}`,
            role: 'assistant',
            content: md,
            intent: intent.value?.intent ?? null,
            citations: citations.value.slice(),
            grounding_ratio: groundingRatio.value,
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
    // 列表
    conversations,
    activeId,
    loadingConversations,
    loadConversations,
    openConversation,
    newConversation,
    removeConversation,
    // 消息
    messages,
    loadingMessages,
    // 回合实时状态
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
    answer,
    citationByMarker,
    verifiedMarkers,
    // 动作
    send,
    abortStream,
    resetTurn,
  }
})
