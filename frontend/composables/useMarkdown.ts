import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'

/**
 * Markdown 渲染 + 引用角标 + MathJax 重排。
 *
 * 三件事分开做，因为它们的时机不同：
 *   - `render()`      纯字符串 → HTML，可在 SSR 跑
 *   - `decorate()`    需要真实 DOM，客户端
 *   - `typeset()`     MathJax 扫 DOM，客户端，且要节流（流式期间每个字都会触发）
 */
const md = new MarkdownIt({
  html: false, // 关掉裸 HTML：正文来自 LLM，不给 XSS 开口子
  linkify: true,
  breaks: true,
})

/** 纯函数，SSR 安全。 */
export function renderMarkdown(source: string): string {
  return DOMPurify.sanitize(md.render(source || ''), { ADD_ATTR: ['data-marker'] })
}

/**
 * 把正文里的 `[1]` / `[1,2]` 换成可点击角标。
 * 幂等：替换后文本节点里已不含 `[n]`，重复调用不会套娃。
 */
export function decorateCitations(root: HTMLElement, verified?: Set<number>): void {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement
      if (!parent) return NodeFilter.FILTER_REJECT
      if (['CODE', 'PRE', 'A'].includes(parent.tagName)) return NodeFilter.FILTER_REJECT
      return /\[\d+(?:\s*[,，]\s*\d+)*\]/.test(node.nodeValue || '')
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_REJECT
    },
  })

  const targets: Text[] = []
  let cur = walker.nextNode()
  while (cur) {
    targets.push(cur as Text)
    cur = walker.nextNode()
  }

  for (const node of targets) {
    const text = node.nodeValue || ''
    const frag = document.createDocumentFragment()
    const re = /\[(\d+(?:\s*[,，]\s*\d+)*)\]/g
    let last = 0
    let m: RegExpExecArray | null

    while ((m = re.exec(text))) {
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)))
      for (const part of m[1].split(/[,，]/)) {
        const n = Number(part.trim())
        if (!Number.isFinite(n)) continue
        const span = document.createElement('span')
        span.className = 'cite-mark' + (verified && !verified.has(n) ? ' is-unverified' : '')
        span.dataset.marker = String(n)
        span.textContent = `[${n}]`
        frag.appendChild(span)
      }
      last = m.index + m[0].length
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)))
    node.parentNode?.replaceChild(frag, node)
  }
}

// MathJax 在流式输出时会被高频调用，攒 250ms 再排一次版
const pending = new Set<HTMLElement>()
let timer: ReturnType<typeof setTimeout> | null = null

export function typesetMath(root: HTMLElement): void {
  if (!import.meta.client) return
  pending.add(root)
  if (timer) return
  timer = setTimeout(() => {
    timer = null
    const targets = [...pending].filter((el) => el.isConnected)
    pending.clear()
    const mj = (window as unknown as { MathJax?: { typesetPromise?: (els: HTMLElement[]) => Promise<unknown> } }).MathJax
    // MathJax 是 CDN 异步加载的，没就绪就跳过；下一次调用会再来一次
    mj?.typesetPromise?.(targets)?.catch(() => {
      /* 公式被流式截断导致排版失败是常态，静默 */
    })
  }, 250)
}

export function useMarkdown() {
  return { render: renderMarkdown, decorate: decorateCitations, typeset: typesetMath }
}
