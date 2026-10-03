/**
 * 直接对着 composables/useMarkdown.ts 跑的渲染自检（不是复刻配置 —— 复刻会漂移）。
 *
 *   npm run check:md
 *
 * 靠 Node 的类型擦除直接 import .ts（Node >= 22.18 默认开启，本项目实际用 22.22）。
 * 这里跑的是**服务端那条路径**：没有 DOM，DOMPurify 被跳过 —— 也就顺带验证了
 * 「SSR 不净化也不开口子」这个前提是否仍然成立（换高亮器最容易把它弄坏）。
 */
import assert from 'node:assert/strict'
import { renderMarkdown } from '../composables/useMarkdown.ts'

const cases = []
const check = (label, fn) => {
  try {
    fn()
    cases.push(`PASS  ${label}`)
  } catch (e) {
    cases.push(`FAIL  ${label}\n      ${e.message}`)
    process.exitCode = 1
  }
}

check('python 围栏 → language-python + hljs 关键字着色', () => {
  const html = renderMarkdown('```python\ndef qsort(a):\n    return a\n```')
  assert.match(html, /<code class="language-python">/)
  assert.match(html, /<span class="hljs-keyword">def<\/span>/)
  assert.match(html, /<span class="hljs-keyword">return<\/span>/)
})

check('未知语言不炸、退化为转义纯文本', () => {
  const html = renderMarkdown('```foobarbaz\nx = 1 < 2\n```')
  assert.match(html, /x = 1 &lt; 2/)
  assert.doesNotMatch(html, /hljs-keyword/)
})

check('无语言围栏退化为转义纯文本', () => {
  assert.match(renderMarkdown('```\na < b\n```'), /a &lt; b/)
})

check('代码块里的 HTML 保持转义（服务端无 DOMPurify 也不开口子）', () => {
  assert.doesNotMatch(renderMarkdown('```python\nx = "<script>alert(1)</script>"\n```'), /<script/)
  // 正文里的裸标签被转义成实体（html:false），所以断言的是"没有真的 <img 标签"，
  // 而不是"文本里不出现 onerror=" —— 后者转义后仍会作为纯文本出现，那是安全的。
  const html = renderMarkdown('正文里的 <img src=x onerror=alert(1)>')
  assert.doesNotMatch(html, /<img/)
  assert.match(html, /&lt;img/)
})

check('行内公式原样保留给 MathJax（nuxt.config 里给 $ 配了定界符）', () => {
  assert.match(renderMarkdown('平均代价为 $O(n \\log n)$。'), /\$O\(n \\log n\)\$/)
})

check('行内代码与引用角标都在（角标交给 decorateCitations）', () => {
  const html = renderMarkdown('见 `sorted(a)` [1,2]。')
  assert.match(html, /<code>sorted\(a\)<\/code>/)
  assert.match(html, /\[1,2\]/)
})

console.log(cases.join('\n'))
console.log(`\n${cases.filter((c) => c.startsWith('PASS')).length}/${cases.length} checks passed`)
