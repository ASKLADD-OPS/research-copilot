import type { Page, Paper, PaperChunk, PaperDetail, PaperStatus, PaperUploadResult } from '~/types/api'

/** 这几个状态说明后台还在干活，前端要继续轮询。 */
export const TRANSIENT_STATUS: readonly PaperStatus[] = ['pending', 'parsing', 'indexing']

export const STATUS_LABEL: Record<PaperStatus, string> = {
  pending: '待处理',
  parsing: '解析中',
  indexing: '入库中',
  ready: '可检索',
  failed: '失败',
}

/**
 * 文献库。
 *
 * 它是"库里有什么"的唯一真源 —— 列表、筛选、上传、解析进度全在这里。
 * 面板只读它的状态，不自己存一份 items，否则删一篇要改三个地方。
 */
export const useLibraryStore = defineStore('library', () => {
  const items = ref<Paper[]>([])
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(50)
  const query = ref('')
  const statusFilter = ref<PaperStatus | ''>('')
  const loading = ref(false)
  /** 首次加载过没有 —— 骨架屏只在第一次出现，翻页时不要再闪一遍 */
  const loaded = ref(false)
  const errorMessage = ref('')

  /**
   * 纳入检索范围的论文 id。
   *
   * **空数组 = 全库**，与后端语义一致（`paper_ids=[]` 表示不限定）。
   * 不要把它初始化为"全部 id"，那样新增一篇论文就不会自动进范围了。
   */
  const scopePaperIds = ref<string[]>([])

  /** 正在轮询的 paper_id → 定时器。解析完成自动清掉。 */
  const pollers = new Map<string, ReturnType<typeof setInterval>>()

  const byId = computed(() => new Map(items.value.map((p) => [p.id, p])))
  const readyItems = computed(() => items.value.filter((p) => p.status === 'ready'))
  const indexingItems = computed(() => items.value.filter((p) => TRANSIENT_STATUS.includes(p.status)))
  const failedItems = computed(() => items.value.filter((p) => p.status === 'failed'))
  const hasMore = computed(() => items.value.length < total.value)

  /** 检索范围真的收窄了吗（空 = 全库）。面板上"全库/已选 N 篇"的判断就用它。 */
  const scopeNarrowed = computed(() => scopePaperIds.value.length > 0)

  function inScope(id: string) {
    return scopePaperIds.value.includes(id)
  }

  function toggleScope(id: string) {
    const i = scopePaperIds.value.indexOf(id)
    if (i >= 0) scopePaperIds.value.splice(i, 1)
    else scopePaperIds.value.push(id)
  }

  function scopeToAll() {
    scopePaperIds.value = []
  }

  /** 把当前页"可检索"的文献全部设为范围 —— 列表里点"仅选这些"时用。 */
  function scopeToReady() {
    scopePaperIds.value = readyItems.value.map((p) => p.id)
  }

  /** 清掉已不在库里的 id，避免删了论文后范围里留着幽灵 id。 */
  function pruneScope() {
    if (!scopePaperIds.value.length || !items.value.length) return
    const alive = new Set(items.value.map((p) => p.id))
    scopePaperIds.value = scopePaperIds.value.filter((id) => alive.has(id))
  }

  // ---------------------------------------------------------------- 读

  /**
   * 拉列表。
   *
   * `reset` 用于翻页/换筛选：page 归 1 并整体替换；否则按"加载更多"追加。
   * 追加模式是刻意的 —— 文献库很容易上百篇，一次 50 条比一次 500 条好。
   */
  async function load(opts: { reset?: boolean } = {}) {
    const api = useApi()
    if (opts.reset) {
      page.value = 1
      items.value = []
    }
    loading.value = true
    errorMessage.value = ''
    try {
      const res = await api.get<Page<Paper>>('/papers', {
        page: page.value,
        page_size: pageSize.value,
        q: query.value || undefined,
        status: statusFilter.value || undefined,
      })
      items.value = opts.reset ? res.items : [...items.value, ...res.items]
      total.value = res.meta.total
      loaded.value = true
      pruneScope()
    } catch (err) {
      errorMessage.value = (err as Error).message
    } finally {
      loading.value = false
    }
  }

  async function fetchDetail(id: string): Promise<PaperDetail> {
    return useApi().get<PaperDetail>(`/papers/${id}`)
  }

  async function fetchChunks(id: string, limit = 200): Promise<PaperChunk[]> {
    return useApi().get<PaperChunk[]>(`/papers/${id}/chunks`, { limit })
  }

  // ---------------------------------------------------------------- 写

  async function upload(file: File, title = ''): Promise<PaperUploadResult> {
    const api = useApi()
    const form = new FormData()
    form.append('file', file)
    form.append('title', title)
    form.append('index', 'true')
    const res = await api.upload<PaperUploadResult>('/papers/upload', form)
    await load({ reset: true })
    startPolling(res.paper.id)
    return res
  }

  async function update(
    id: string,
    patch: Partial<Pick<Paper, 'title' | 'abstract' | 'year' | 'venue' | 'tags'>>,
  ) {
    const updated = await useApi().patch<Paper>(`/papers/${id}`, patch)
    const i = items.value.findIndex((p) => p.id === id)
    if (i >= 0) items.value[i] = updated
    return updated
  }

  async function reindex(id: string) {
    const res = await useApi().post<PaperUploadResult>(`/papers/${id}/reindex`)
    startPolling(id)
    return res
  }

  async function remove(id: string) {
    stopPolling(id)
    await useApi().del<Record<string, unknown>>(`/papers/${id}`)
    items.value = items.value.filter((p) => p.id !== id)
    total.value = Math.max(0, total.value - 1)
    pruneScope()
  }

  function pdfUrl(id: string) {
    return useApi().buildUrl(`/papers/${id}/file`)
  }

  // ---------------------------------------------------------------- 轮询

  /**
   * 轮询单篇论文直到解析结束。
   *
   * 为什么是轮询而不是推送：解析在 backend 进程内的线程里跑，与请求之间没有
   * 现成的推送通道（去掉了 Redis/Celery 之后更是如此）。3 秒一次、最多 10 分钟，
   * 对一个"上传完就该去干别的"的场景完全够用。
   */
  function startPolling(id: string, intervalMs = 3000, timeoutMs = 600_000) {
    stopPolling(id)
    const startedAt = Date.now()
    const timer = setInterval(async () => {
      // 页面切走 / 标签页隐藏时不要空转，白烧后端连接
      if (import.meta.client && document.hidden) return
      if (Date.now() - startedAt > timeoutMs) {
        stopPolling(id)
        return
      }
      try {
        const detail = await fetchDetail(id)
        const i = items.value.findIndex((p) => p.id === id)
        if (i >= 0) items.value[i] = { ...items.value[i], ...detail }
        if (!TRANSIENT_STATUS.includes(detail.status)) {
          stopPolling(id)
          await load({ reset: true })
        }
      } catch {
        stopPolling(id) // 论文被删或后端重启，别再空转
      }
    }, intervalMs)
    pollers.set(id, timer)
  }

  function stopPolling(id: string) {
    const t = pollers.get(id)
    if (t) clearInterval(t)
    pollers.delete(id)
  }

  function stopAllPolling() {
    for (const id of [...pollers.keys()]) stopPolling(id)
  }

  /** 上传完就切走页面的情况：重进工作台时把没跑完的接着轮询上。 */
  function resumePolling() {
    for (const p of indexingItems.value) {
      if (!pollers.has(p.id)) startPolling(p.id)
    }
  }

  return {
    items,
    total,
    page,
    pageSize,
    query,
    statusFilter,
    loading,
    loaded,
    errorMessage,
    scopePaperIds,
    byId,
    readyItems,
    indexingItems,
    failedItems,
    hasMore,
    scopeNarrowed,
    inScope,
    toggleScope,
    scopeToAll,
    scopeToReady,
    pruneScope,
    load,
    fetchDetail,
    fetchChunks,
    upload,
    update,
    reindex,
    remove,
    pdfUrl,
    startPolling,
    stopPolling,
    stopAllPolling,
    resumePolling,
  }
})
