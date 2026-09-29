import type { HealthReport, ToolInfo, ToolListOut } from '~/types/api'

/** 全局 UI 状态：后端健康、工具清单、检索范围选择。 */
export const useUiStore = defineStore('ui', () => {
  const health = ref<HealthReport | null>(null)
  const healthError = ref('')
  const checkingHealth = ref(false)

  const tools = ref<ToolInfo[]>([])
  const enabledServers = ref<string[]>([])
  const toolsNote = ref('')
  const loadingTools = ref(false)

  /** 对话页的检索范围：选中的论文 id（空 = 全库）。 */
  const scopePaperIds = ref<string[]>([])

  // 看板参数，跟后端默认值保持一致
  const topK = ref<number | null>(null)
  const debugMode = ref(false)

  /**
   * 主题。暗色是设计规范的本源（linear.app 的 canvas 是 #010102），所以默认暗色；
   * 浅色那套 token 在 tokens.css 里，切换只换 <html data-theme>。
   */
  const THEME_KEY = 'rc-theme'
  const theme = ref<'dark' | 'light'>('dark')

  function setTheme(next: 'dark' | 'light') {
    theme.value = next
    if (import.meta.client) localStorage.setItem(THEME_KEY, next)
  }

  function toggleTheme() {
    setTheme(theme.value === 'dark' ? 'light' : 'dark')
  }

  /** SSR 拿不到 localStorage，挂载后补读一次。 */
  function restoreTheme() {
    if (!import.meta.client) return
    const saved = localStorage.getItem(THEME_KEY)
    if (saved === 'light' || saved === 'dark') theme.value = saved
  }

  async function refreshHealth() {
    const config = useRuntimeConfig()
    // /health 挂在根路径上，不在 /api/v1 下
    const root = (config.public.apiBase as string).replace(/\/api\/v\d+\/?$/, '')
    checkingHealth.value = true
    healthError.value = ''
    try {
      const res = await fetch(`${root}/health`, { headers: { Accept: 'application/json' } })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      health.value = (await res.json()) as HealthReport
    } catch (err) {
      health.value = null
      healthError.value = (err as Error).message
    } finally {
      checkingHealth.value = false
    }
  }

  async function loadTools() {
    const api = useApi()
    loadingTools.value = true
    try {
      const res = await api.get<ToolListOut>('/tools')
      tools.value = res.tools
      enabledServers.value = res.enabled_servers
      toolsNote.value = res.note
    } catch (err) {
      toolsNote.value = (err as Error).message
    } finally {
      loadingTools.value = false
    }
  }

  const healthLight = computed<'ok' | 'degraded' | 'down' | 'unknown'>(() => {
    if (healthError.value) return 'down'
    return health.value?.status ?? 'unknown'
  })

  return {
    health,
    healthError,
    checkingHealth,
    healthLight,
    refreshHealth,
    tools,
    enabledServers,
    toolsNote,
    loadingTools,
    loadTools,
    scopePaperIds,
    topK,
    debugMode,
    theme,
    setTheme,
    toggleTheme,
    restoreTheme,
  }
})
