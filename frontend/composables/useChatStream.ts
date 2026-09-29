import type { StreamEvent, StreamEventName } from '~/types/api'

/**
 * SSE 消费器（POST + 流式）。
 *
 * 为什么不用 `EventSource`：它只支持 GET，而对话要 POST 一个 JSON body
 * （query / paper_ids / conversation_id）。所以这里用 fetch + ReadableStream
 * 手动解 SSE 帧。
 *
 * SSE 帧格式（见 backend/app/llm/streaming.py）：
 *     event: token
 *     data: {"text": "..."}
 *     <空行>
 *
 * 注意 `data:` 会有多行（正文里带换行时后端逐行加了前缀），
 * 所以要按行收集后用 `\n` 拼回去，不能只取第一行。
 */
export interface StreamHandlers {
  onEvent: (evt: StreamEvent) => void
  onError?: (err: Error) => void
  onClose?: () => void
}

export function useChatStream() {
  const config = useRuntimeConfig()
  const sseBase = config.public.sseBase as string
  let controller: AbortController | null = null

  function abort() {
    controller?.abort()
    controller = null
  }

  /** 解析一个完整的 SSE 帧块（已按空行切开）。 */
  function parseFrame(block: string): StreamEvent | null {
    let name = 'message'
    const dataLines: string[] = []

    for (const rawLine of block.split('\n')) {
      const line = rawLine.replace(/\r$/, '')
      if (!line || line.startsWith(':')) continue // 注释帧（保活）
      const idx = line.indexOf(':')
      const field = idx === -1 ? line : line.slice(0, idx)
      const value = idx === -1 ? '' : line.slice(idx + 1).replace(/^ /, '')

      if (field === 'event') name = value
      else if (field === 'data') dataLines.push(value)
    }

    if (!dataLines.length) return null
    const payload = dataLines.join('\n')
    let data: unknown = payload
    try {
      data = JSON.parse(payload)
    } catch {
      // 保持原字符串，token 事件理论上永远是 JSON，但别为此丢数据
    }
    return { event: name as StreamEventName, data }
  }

  async function start(body: unknown, handlers: StreamHandlers): Promise<void> {
    abort()
    controller = new AbortController()
    const url = sseBase.replace(/\/$/, '') + '/chat/stream'

    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify(body),
        signal: controller.signal,
      })

      if (!res.ok) {
        const text = await res.text().catch(() => '')
        throw new Error(`流式接口返回 HTTP ${res.status}：${text.slice(0, 300)}`)
      }
      if (!res.body) throw new Error('响应没有 body，无法读取流')

      const reader = res.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // SSE 以空行分帧。最后一段可能不完整，留在 buffer 里等下一块。
        let sep = buffer.indexOf('\n\n')
        while (sep !== -1) {
          const block = buffer.slice(0, sep)
          buffer = buffer.slice(sep + 2)
          const evt = parseFrame(block)
          if (evt) handlers.onEvent(evt)
          sep = buffer.indexOf('\n\n')
        }
      }

      // 收尾：有些服务端不会给最后一个空行
      const tail = parseFrame(buffer.trim())
      if (tail) handlers.onEvent(tail)
    } catch (err) {
      if ((err as Error).name === 'AbortError') return // 用户主动停止，不算错误
      handlers.onError?.(err as Error)
    } finally {
      controller = null
      handlers.onClose?.()
    }
  }

  return { start, abort, get streaming() { return controller !== null } }
}
