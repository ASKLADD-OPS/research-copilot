import tailwindcss from '@tailwindcss/vite'

// Nuxt 3 配置。
// runtimeConfig.public 的两个键会被环境变量自动覆盖，无需在代码里读 process.env：
//   NUXT_PUBLIC_API_BASE → public.apiBase
//   NUXT_PUBLIC_SSE_BASE → public.sseBase
//
// 样式：Tailwind CSS v4 走 Vite 插件接入（v4 不再需要 tailwind.config.js 与 postcss 配置），
// 设计 token 全部写在 assets/css/main.css 的 @theme 里 —— 那是唯一的真源。
// 没有 UI 组件库：按钮/输入框是工具类，只有"带行为"的东西（可拖拽分割线、面板壳、
// 模态框）才抽成 components/ui 下的组件。
export default defineNuxtConfig({
  compatibilityDate: '2025-01-01',
  devtools: { enabled: false },

  modules: ['@pinia/nuxt'],

  // pathPrefix: false —— 让 components/ui/AppPanel.vue 直接用 <AppPanel> 而不是 <UiAppPanel>。
  // 这些基础组件的目录只是"文件归类"，不该渗进模板里的名字。
  components: [{ path: '~/components', pathPrefix: false }],

  css: ['~/assets/css/main.css'],

  vite: {
    plugins: [tailwindcss()],
  },

  runtimeConfig: {
    public: {
      // 浏览器直连后端，所以默认值是 localhost 而不是容器名
      apiBase: 'http://localhost:8000/api/v1',
      sseBase: 'http://localhost:8000/api/v1',
    },
  },

  app: {
    head: {
      title: 'Research Copilot · 多智能体学术研究工作台',
      htmlAttrs: { lang: 'zh-CN' },
      meta: [
        { charset: 'utf-8' },
        { name: 'viewport', content: 'width=device-width, initial-scale=1' },
        {
          name: 'description',
          content:
            '面向科研语料的多智能体研究助手：PDF 入库、混合检索、跨篇推理、引文图谱与写作辅助，回答带句级溯源。',
        },
      ],
      link: [
        // Inter 走 CDN：它是这套设计的排版骨架，但装成本地依赖要带十几个 woff2 分片，
        // 而 preconnect + 一个 css 就够。font-display: swap 由 Google Fonts 自身带上。
        { rel: 'preconnect', href: 'https://fonts.googleapis.com' },
        { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: '' },
        {
          rel: 'stylesheet',
          href: 'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap',
        },
      ],
      script: [
        /*
         * MathJax 3 的 `tex-mml-chtml` 默认**只认 `\(...\)` 行内公式，不认 `$...$`**，
         * 而 LLM 输出的行内公式几乎都用 `$...$` —— 实测「其平均开销为 $O(n\log n)$。」
         * 原样显示成了带美元符号的纯文本。所以先写这份配置，再加载 MathJax。
         * 顺序是关键：内联脚本同步执行，MathJax（async）读 window.MathJax 时它已经在了。
         */
        {
          innerHTML: String.raw`window.MathJax={tex:{inlineMath:[["$","$"],["\\(","\\)"]]}}`,
        },
        // MathJax 走 CDN：省掉 ~10MB 的 node 依赖，且只在首屏后异步加载
        {
          src: 'https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js',
          async: true,
        },
      ],
    },
  },

  /*
   * 渲染策略。
   *
   * 营销页要 SSR —— 搜索引擎必须能拿到完整 HTML（/blog 上线后尤其如此）。
   * 工作台也**保持 SSR**：它的首屏骨架是服务端渲染出来的，关了会白屏（功能退化）。
   * 不收录用页面里的 <meta name="robots" content="noindex"> 解决，不用关 SSR。
   */
  routeRules: {
    '/': { ssr: true },
    '/pricing': { ssr: true },
    '/about': { ssr: true },
    '/app/**': { ssr: true },
  },
})
