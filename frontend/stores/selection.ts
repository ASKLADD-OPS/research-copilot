import type { PaperChunk, PaperDetail } from '~/types/api'
import type { ReaderSelection, ReaderTarget } from '~/types/workbench'

/**
 * 自增的跳转序号。
 *
 * 不能用 `Date.now()`：连点两下同一个引用角标，毫秒值可能一样，
 * 那样 `target` 的引用变了但内容看着没变，阅读器那边的 watch 就不会再滚一次。
 */
let targetSeq = 0

/**
 * 选中文献 + 阅读锚点。
 *
 * 这个 store 存在的理由是多面板：左侧列表点一篇 → 中栏阅读器换文档 →
 * 右栏对话把这篇设为检索范围。三处都要读同一个"当前选中"，靠 props 传会变成
 * 三层的 props 接力，靠事件总线又会丢状态。
 */
export const useSelectionStore = defineStore('selection', () => {
  const activeId = ref<string | null>(null)
  const detail = ref<PaperDetail | null>(null)
  const chunks = ref<PaperChunk[]>([])
  const loading = ref(false)
  const errorMessage = ref('')

  /** 阅读器当前页（1-based）。由 PdfCanvas 回写 —— 供"追问这一页"与状态栏用。 */
  const page = ref(1)
  /** 跳转指令。PdfCanvas 只认它的 nonce。 */
  const target = ref<ReaderTarget | null>(null)
  /** 划词记录，最新的在末尾。 */
  const selections = ref<ReaderSelection[]>([])

  const active = computed(() => detail.value)
  const latestSelection = computed<ReaderSelection | null>(() => selections.value.at(-1) ?? null)
  const hasPdf = computed(() => Boolean(detail.value?.pdf_path))
  const selectionCount = computed(() => selections.value.length)

  function isActive(id: string | null | undefined) {
    return Boolean(id) && id === activeId.value
  }

  /** 当前页覆盖到的分块 —— 检视面板里"这页有什么"用。 */
  const pageChunks = computed(() =>
    chunks.value.filter((c) => {
      const s = c.page_start ?? 0
      const e = c.page_end ?? s
      return page.value >= s && page.value <= e
    }),
  )

  async function select(id: string | null) {
    if (!id) {
      clear()
      return
    }
    if (id === activeId.value && detail.value) return

    loading.value = true
    errorMessage.value = ''
    activeId.value = id
    page.value = 1
    chunks.value = []
    try {
      const api = useApi()
      // 详情与分块并发取：分块用于检视面板与"这页有什么"，详情用于标题与 PDF 路径。
      // 分块失败不该让整次选中失败，所以单独 catch。
      const [d, c] = await Promise.all([
        api.get<PaperDetail>(`/papers/${id}`),
        api.get<PaperChunk[]>(`/papers/${id}/chunks`, { limit: 200 }).catch(() => [] as PaperChunk[]),
      ])
      detail.value = d
      chunks.value = c
    } catch (err) {
      errorMessage.value = (err as Error).message
      detail.value = null
    } finally {
      loading.value = false
    }
  }

  /** 选中并跳到某页。引用角标、图谱节点、划词追问都走这里。 */
  async function openAt(paperId: string, pageNum: number, quote?: string) {
    await select(paperId)
    target.value = { paperId, page: pageNum, quote, nonce: ++targetSeq }
  }

  /** 只跳页，不换文档。用于阅读器自己的翻页与"返回上次位置"。 */
  function requestPage(pageNum: number, quote?: string) {
    if (!activeId.value) return
    target.value = {
      paperId: activeId.value,
      page: Math.max(1, Math.floor(pageNum)),
      quote,
      nonce: ++targetSeq,
    }
  }

  /** PdfCanvas 渲染完真实页码后回写。不回写的话"追问这一页"会拿到过期的页号。 */
  function setPage(n: number) {
    if (Number.isFinite(n) && n >= 1) page.value = Math.floor(n)
  }

  function pushSelection(sel: Omit<ReaderSelection, 'createdAt'>) {
    const text = sel.text.trim()
    if (!text) return
    // 同一页同一段文字重复划选只更新时间，不堆重复项
    const dup = selections.value.findIndex(
      (s) => s.paperId === sel.paperId && s.page === sel.page && s.text === text,
    )
    const item: ReaderSelection = { ...sel, text, createdAt: Date.now() }
    if (dup >= 0) selections.value.splice(dup, 1, item)
    else selections.value.push(item)
    if (selections.value.length > 50) selections.value.splice(0, selections.value.length - 50)
  }

  function dropSelection(createdAt: number) {
    selections.value = selections.value.filter((s) => s.createdAt !== createdAt)
  }

  function clearSelections() {
    selections.value = []
  }

  function clear() {
    activeId.value = null
    detail.value = null
    chunks.value = []
    page.value = 1
    target.value = null
    errorMessage.value = ''
  }

  return {
    activeId,
    detail,
    active,
    chunks,
    pageChunks,
    loading,
    errorMessage,
    page,
    target,
    selections,
    latestSelection,
    hasPdf,
    selectionCount,
    isActive,
    select,
    openAt,
    requestPage,
    setPage,
    pushSelection,
    dropSelection,
    clearSelections,
    clear,
  }
})
