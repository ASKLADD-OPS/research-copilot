import type { HealthReport, ToolInfo, ToolListOut } from '~/types/api'
import type {
  MainView,
  PanelKey,
  TaskOut,
  WorkTab,
  WorkbenchLayout,
} from '~/types/workbench'

const LAYOUT_KEY = 'rc-layout'

/** 面板宽度的硬边界。拖拽时会 clamp 到这两个值之间。 */
export const PANEL_BOUNDS: Record<'library' | 'work', [number, number]> = {
  library: [200, 440],
  work: [300, 620],
}

const DEFAULT_LAYOUT: WorkbenchLayout = {
  mainView: 'reader',
  workTab: 'chat',
  library: { size: 268, collapsed: false },
  work: { size: 388, collapsed: false },
}

function clamp(v: number, [min, max]: [number, number]) {
  return Math.min(max, Math.max(min, v))
}

/**
 * 应用外壳状态：三栏布局、后端健康、工具清单、后台任务。
 *
 * 布局能持久化是这个 store 的第一职责 —— 一个多面板工作台每次打开都回到默认宽度，
 * 用户会骂人。面板宽度存的是像素，折叠时**不归零**，这样展开能还原成原样。
 */
export const useUiStore = defineStore('ui', () => {
  const layout = ref<WorkbenchLayout>(structuredClone(DEFAULT_LAYOUT))
  const layoutReady = ref(false)

  // ---------------------------------------------------------------- 布局

  function setPanelSize(key: 'library' | 'work', size: number) {
    layout.value[key] = { ...layout.value[key], size: clamp(size, PANEL_BOUNDS[key]) }
  }

  function setPanelCollapsed(key: PanelKey, collapsed: boolean) {
    if (key === 'main') return // 中栏是主体，不允许折叠
    layout.value[key] = { ...layout.value[key], collapsed }
  }

  function togglePanel(key: PanelKey) {
    if (key === 'main') return
    layout.value[key] = { ...layout.value[key], collapsed: !layout.value[key].collapsed }
  }

  function isCollapsed(key: PanelKey) {
    return key === 'main' ? false : layout.value[key].collapsed
  }

  const libraryWidth = computed(() =>
    layout.value.library.collapsed ? 0 : layout.value.library.size,
  )
  const workWidth = computed(() => (layout.value.work.collapsed ? 0 : layout.value.work.size))

  function setMainView(v: MainView) {
    layout.value.mainView = v
  }

  function setWorkTab(t: WorkTab) {
    layout.value.workTab = t
    // 切到某个标签默认就是"我要用它"，顺手展开右栏 —— 否则点了没反应像坏了
    if (layout.value.work.collapsed) setPanelCollapsed('work', false)
  }

  function resetLayout() {
    layout.value = structuredClone(DEFAULT_LAYOUT)
  }

  function restoreLayout() {
    if (!import.meta.client) return
    try {
      const raw = localStorage.getItem(LAYOUT_KEY)
      if (raw) {
        const saved = JSON.parse(raw) as Partial<WorkbenchLayout>
        layout.value = {
          ...structuredClone(DEFAULT_LAYOUT),
          ...saved,
          library: { ...DEFAULT_LAYOUT.library, ...(saved.library ?? {}) },
          work: { ...DEFAULT_LAYOUT.work, ...(saved.work ?? {}) },
        }
      }
    } catch {
      /* 存档坏了就用默认值，不值得为此打断启动 */
    } finally {
      layoutReady.value = true
    }
  }

  if (import.meta.client) {
    watch(
      layout,
      (v) => {
        try {
          localStorage.setItem(LAYOUT_KEY, JSON.stringify(v))
        } catch {
          /* 隐私模式禁写 */
        }
      },
      { deep: true },
    )
  }

  // ---------------------------------------------------------------- 健康

  const health = ref<HealthReport | null>(null)
  const healthError = ref('')
  const checkingHealth = ref(false)

  const healthLight = computed<'ok' | 'degraded' | 'down' | 'unknown'>(() => {
    if (healthError.value) return 'down'
    return health.value?.status ?? 'unknown'
  })

  const unhealthy = computed(() => (health.value?.components ?? []).filter((c) => !c.ok))

  async function refreshHealth() {
    const config = useRuntimeConfig()
    // /health 挂在根路径上，不在 /api/v1 下
    const root = (config.public.apiBase as string).replace(/\/api\/v\d+\/?$/, '')
    checkingHealth.value = true
    healthError.value = ''
    try {
      const res = await fetch(`${root}/health`, { headers: { Accept: 'application/json' } })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const body = (await res.json()) as { data: HealthReport }
      health.value = body.data ?? null
    } catch (err) {
      health.value = null
      healthError.value = (err as Error).message
    } finally {
      checkingHealth.value = false
    }
  }

  // ---------------------------------------------------------------- 工具

  const tools = ref<ToolInfo[]>([])
  const enabledServers = ref<string[]>([])
  const toolsNote = ref('')
  const loadingTools = ref(false)

  async function loadTools() {
    loadingTools.value = true
    try {
      const res = await useApi().get<ToolListOut>('/tools')
      tools.value = res.tools
      enabledServers.value = res.enabled_servers
      toolsNote.value = res.note
    } catch (err) {
      toolsNote.value = (err as Error).message
    } finally {
      loadingTools.value = false
    }
  }

  // ---------------------------------------------------------------- 后台任务

  const tasks = ref<TaskOut[]>([])
  const loadingTasks = ref(false)

  /** 还没跑完的任务。状态栏只展示这些，跑完的不占地方。 */
  const activeTasks = computed(() => tasks.value.filter((t) => t.status === 'pending' || t.status === 'running'))
  const failedTasks = computed(() => tasks.value.filter((t) => t.status === 'failed'))

  async function loadTasks() {
    loadingTasks.value = true
    try {
      tasks.value = await useApi().get<TaskOut[]>('/tasks', { limit: 20 })
    } catch {
      /* 状态栏是装饰性的，取不到就静默留空，不要弹错误打断用户 */
    } finally {
      loadingTasks.value = false
    }
  }

  // ---------------------------------------------------------------- 检索看板
  //
  // 这里刻意**没有** topK / debug 开关。它们曾经存在，但 /chat/stream 的请求体
  // （ChatRequest）只接受 query / conversation_id / paper_ids / intent，
  // 给一个改了不生效的滑块比不给更糟。

  return {
    layout,
    layoutReady,
    libraryWidth,
    workWidth,
    setPanelSize,
    setPanelCollapsed,
    togglePanel,
    isCollapsed,
    setMainView,
    setWorkTab,
    resetLayout,
    restoreLayout,

    health,
    healthError,
    checkingHealth,
    healthLight,
    unhealthy,
    refreshHealth,

    tools,
    enabledServers,
    toolsNote,
    loadingTools,
    loadTools,

    tasks,
    loadingTasks,
    activeTasks,
    failedTasks,
    loadTasks,
  }
})
