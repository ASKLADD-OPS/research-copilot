import MarkdownIt from 'markdown-it'
import createDOMPurify from 'dompurify'
// 只要 common 那 40 来个常用语言，不是全量 190+ 语言包（全量 ~1MB，这里 ~180KB）
import hljs from 'highlight.js/lib/common'

/**
 * Markdown 渲染 + 引用角标 + MathJax 重排。
 *
 * 三件事分开做，因为它们的时机不同：
 *   - `render()`      纯字符串 → HTML，**服务端与客户端都能跑**（见下面的 purifier 说明）
 *   - `decorate()`    需要真实 DOM，客户端
 *   - `typeset()`     MathJax 扫 DOM，客户端，且要节流（流式期间每个字都会触发）
 */
const md = new MarkdownIt({
  html: false, // 关掉裸 HTML：正文来自 LLM，不给 XSS 开口子
  linkify: true,
  breaks: true,
  /**
   * 代码高亮。返回空串 = 交给 markdown-it 自己转义（比返回未经转义的原码安全，
   * 也比 highlightAuto 猜语言快 —— 猜错比不猜更难看）。hljs 的 .value 自带转义。
   */
  highlight(code, lang) {
    if (!lang || !hljs.getLanguage(lang)) return ''
    return hljs.highlight(code, { language: lang, ignoreIllegals: true }).value
  },
})

/**
 * SSR 守卫 —— 这里曾经有一个只在生产才爆的坑。
 *
 * `dompurify` 的默认导出在**没有 DOM 的环境里是工厂函数，不是实例**：
 * `isSupported === false`、`.sanitize` 是 `undefined`。直接调用会抛
 * `TypeError: DOMPurify.sanitize is not a function`。
 *
 * 隐蔽之处在于"什么时候才会被调到"：工作台的聊天首屏没有消息，
 * 所以 SSR 期间 `renderMarkdown` 从未真正执行 —— typecheck、build、
 * SSR 冒烟**全都绿**。等到多了一个"每次请求都要渲染正文"的页面
 * （比如博客详情），才会变成每请求 500。
 *
 * 服务端跳过净化是**安全**的：上面 markdown-it 配了 `html: false`，
 * 裸标签已被转义成实体（`<script>` → `&lt;script&gt;`），没有注入面。
 * 净化本身是纵深防御，留到客户端做即可。
 */
const purifier =
  typeof (createDOMPurify as unknown as { sanitize?: unknown }).sanitize === 'function'
    ? createDOMPurify
    : null

/** 纯函数，SSR 安全（服务端自动跳过净化，见 purifier 注释）。 */
export function renderMarkdown(source: string): string {
  const html = md.render(source || '')
  return purifier ? purifier.sanitize(html, { ADD_ATTR: ['data-marker'] }) : html
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
