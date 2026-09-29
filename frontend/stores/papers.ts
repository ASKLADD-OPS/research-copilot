import type { Page, Paper, PaperChunk, PaperDetail, PaperStatus, PaperUploadResult } from '~/types/api'

/** 解析中的状态：这几个状态下前端要轮询详情。 */
const TRANSIENT: PaperStatus[] = ['pending', 'parsing', 'indexing']

export const usePapersStore = defineStore('papers', () => {
  const items = ref<Paper[]>([])
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(20)
  const query = ref('')
  const statusFilter = ref<PaperStatus | ''>('')
  const loading = ref(false)
  const errorMessage = ref('')

  /** 正在轮询的 paper_id → 定时器。解析完成后自动清掉。 */
  const pollers = new Map<string, ReturnType<typeof setInterval>>()

  async function load() {
    const api = useApi()
    loading.value = true
    errorMessage.value = ''
    try {
      const res = await api.get<Page<Paper>>('/papers', {
        page: page.value,
        page_size: pageSize.value,
        q: query.value || undefined,
        status: statusFilter.value || undefined,
      })
      items.value = res.items
      total.value = res.meta.total
    } catch (err) {
      errorMessage.value = (err as Error).message
    } finally {
      loading.value = false
    }
  }

  async function fetchDetail(id: string): Promise<PaperDetail> {
    const api = useApi()
    return api.get<PaperDetail>(`/papers/${id}`)
  }

  async function fetchChunks(id: string, limit = 100): Promise<PaperChunk[]> {
    const api = useApi()
    return api.get<PaperChunk[]>(`/papers/${id}/chunks`, { limit })
  }

  /** 上传。返回后端结果，由调用方决定要不要轮询。 */
  async function upload(file: File, title = ''): Promise<PaperUploadResult> {
    const api = useApi()
    const form = new FormData()
    form.append('file', file)
    form.append('title', title)
    form.append('index', 'true')
    const res = await api.upload<PaperUploadResult>('/papers/upload', form)
    await load()
    startPolling(res.paper.id)
    return res
  }

  /**
   * 轮询单篇论文直到解析完成。
   *
   * 为什么要轮询而不是 WebSocket：解析要几十秒，Celery 与 web 进程之间
   * 没有现成的推送通道，为此加一条 WS 不划算。3 秒一次、最长 10 分钟足够。
   */
  function startPolling(id: string, intervalMs = 3000, timeoutMs = 600_000) {
    stopPolling(id)
    const startedAt = Date.now()
    const timer = setInterval(async () => {
      if (Date.now() - startedAt > timeoutMs) {
        stopPolling(id)
        return
      }
      try {
        const detail = await fetchDetail(id)
        const idx = items.value.findIndex((p) => p.id === id)
        if (idx >= 0) items.value[idx] = { ...items.value[idx], ...detail }
        if (!TRANSIENT.includes(detail.status)) {
          stopPolling(id)
          await load()
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

  async function update(id: string, patch: Partial<Pick<Paper, 'title' | 'abstract' | 'year' | 'venue' | 'tags'>>) {
    const api = useApi()
    const updated = await api.patch<Paper>(`/papers/${id}`, patch)
    const idx = items.value.findIndex((p) => p.id === id)
    if (idx >= 0) items.value[idx] = updated
    return updated
  }

  async function reindex(id: string) {
    const api = useApi()
    const res = await api.post<PaperUploadResult>(`/papers/${id}/reindex`)
    startPolling(id)
    return res
  }

  async function remove(id: string) {
    const api = useApi()
    stopPolling(id)
    await api.del(`/papers/${id}`)
    items.value = items.value.filter((p) => p.id !== id)
    total.value = Math.max(0, total.value - 1)
  }

  /** PDF 直链，交给 PdfViewer / 下载按钮用。 */
  function pdfUrl(id: string) {
    const api = useApi()
    return api.buildUrl(`/papers/${id}/file`)
  }

  const readyCount = computed(() => items.value.filter((p) => p.status === 'ready').length)

  return {
    items,
    total,
    page,
    pageSize,
    query,
    statusFilter,
    loading,
    errorMessage,
    readyCount,
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
  }
})
