// Nuxt 3 配置。
// runtimeConfig.public 的两个键会被环境变量自动覆盖，无需在代码里读 process.env：
//   NUXT_PUBLIC_API_BASE → public.apiBase
//   NUXT_PUBLIC_SSE_BASE → public.sseBase
//
// 没有 UI 组件库：视觉层由 assets/css/tokens.css（Linear token）+ main.css（共享类）
// 承担，模态/抽屉/分段控件是 components/ui 下的三个原生组件。见 docs/技术文档.md。
export default defineNuxtConfig({
  compatibilityDate: '2025-01-01',
  devtools: { enabled: false },

  modules: ['@pinia/nuxt'],

  // pathPrefix: false —— 让 components/ui/Modal.vue 直接用 <Modal> 而不是 <UiModal>。
  // 这些基础组件的目录只是"文件归类"，不该渗进模板里的名字。
  components: [{ path: '~/components', pathPrefix: false }],

  css: ['~/assets/css/tokens.css', '~/assets/css/main.css'],

  runtimeConfig: {
    public: {
      // 浏览器直连后端，所以默认值是 localhost 而不是容器名
      apiBase: 'http://localhost:8000/api/v1',
      sseBase: 'http://localhost:8000/api/v1',
    },
  },

  app: {
    head: {
      title: 'Research Copilot',
      // data-theme 必须在 SSR 的输出里就带上，否则首屏会先闪一下暗色默认值
      htmlAttrs: { lang: 'zh-CN', 'data-theme': 'dark' },
      meta: [
        { charset: 'utf-8' },
        { name: 'viewport', content: 'width=device-width, initial-scale=1' },
        { name: 'description', content: '多智能体学术研究助手：检索 · 问答 · 引文图谱 · 写作' },
      ],
      script: [
        // MathJax 走 CDN：省掉 ~10MB 的 node 依赖，且只在首屏后异步加载
        {
          src: 'https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js',
          async: true,
        },
      ],
    },
  },

  // 长文本与 SSR 的取舍：ECharts / PDF.js 都是浏览器重组件，统一放 <ClientOnly> 里，
  // 页面本身保持可 SSR（首屏骨架 + SEO 友好）。
  routeRules: {
    '/graph': { ssr: false }, // 图谱页 100% 依赖 canvas
  },
})
