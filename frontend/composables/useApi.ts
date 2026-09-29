import type { ApiResponse } from '~/types/api'

/**
 * 后端调用统一入口。
 *
 * 后端所有接口都返回 `{code, data, message}`，所以"拆信封"这件事只在这里做一次：
 * 调用方拿到的是 `data`，出错时抛 `ApiError`。这样每个页面里不会到处是
 * `if (res.code !== 0)`。
 */
export class ApiError extends Error {
  constructor(
    readonly code: number,
    message: string,
    readonly status?: number,
  ) {
    super(message)
    this.name = 'ApiError'
    // 让 instanceof 在编译到 ES5 时也成立
    Object.setPrototypeOf(this, ApiError.prototype)
  }
}

type Query = Record<string, string | number | boolean | undefined | null | string[]>

export function useApi() {
  const config = useRuntimeConfig()
  const base = config.public.apiBase as string

  function buildUrl(path: string, query?: Query): string {
    const url = new URL(base.replace(/\/$/, '') + (path.startsWith('/') ? path : `/${path}`))
    if (query) {
      for (const [k, v] of Object.entries(query)) {
        if (v === undefined || v === null || v === '') continue
        if (Array.isArray(v)) v.forEach((item) => url.searchParams.append(k, item))
        else url.searchParams.append(k, String(v))
      }
    }
    return url.toString()
  }

  async function unwrap<T>(res: Response): Promise<T> {
    if (res.status === 204) return undefined as T
    const text = await res.text()
    let body: ApiResponse<T> | null = null
    try {
      body = text ? (JSON.parse(text) as ApiResponse<T>) : null
    } catch {
      // 后端崩了会返回 HTML 错误页，别让 JSON.parse 的报错盖掉真正的信息
      throw new ApiError(-1, `响应不是合法 JSON（HTTP ${res.status}）：${text.slice(0, 200)}`, res.status)
    }
    if (!res.ok) {
      throw new ApiError(body?.code ?? res.status, body?.message || res.statusText, res.status)
    }
    if (!body) throw new ApiError(-1, '空响应')
    if (body.code !== 0) throw new ApiError(body.code, body.message || '请求失败', res.status)
    return body.data as T
  }

  async function request<T>(path: string, init: RequestInit & { query?: Query } = {}): Promise<T> {
    const { query, ...rest } = init
    const isForm = rest.body instanceof FormData
    const res = await fetch(buildUrl(path, query), {
      ...rest,
      headers: {
        Accept: 'application/json',
        // FormData 要让浏览器自己带 boundary，手写 Content-Type 会把请求弄坏
        ...(isForm ? {} : { 'Content-Type': 'application/json' }),
        ...(rest.headers as Record<string, string> | undefined),
      },
    })
    return unwrap<T>(res)
  }

  return {
    base,
    buildUrl,

    get: <T>(path: string, query?: Query) => request<T>(path, { method: 'GET', query }),

    post: <T>(path: string, body?: unknown, query?: Query) =>
      request<T>(path, {
        method: 'POST',
        query,
        body: body === undefined ? undefined : JSON.stringify(body),
      }),

    patch: <T>(path: string, body?: unknown) =>
      request<T>(path, { method: 'PATCH', body: body === undefined ? undefined : JSON.stringify(body) }),

    del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),

    upload: <T>(path: string, form: FormData) => request<T>(path, { method: 'POST', body: form }),
  }
}

/** 把字节数格式化成人类可读。 */
export function formatBytes(n?: number | null): string {
  if (!n || n <= 0) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`
}

/** ISO 时间 → 本地短格式。 */
export function formatTime(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString('zh-CN', { hour12: false })
}
