<script setup lang="ts">
/**
 * 营销首页。
 *
 * 结构：Hero → 功能区 → 使用场景 → 实时演示（SSE）→ FAQ → 结尾 CTA。
 * 用命名 layout 而不是把导航写在这里 —— 定价/关于页要复用同一套页头页脚。
 *
 * 文案全部基于本项目的**真实实现**，不写没有的东西：
 * 说"句级溯源"是因为 `rag/source_tracing.py` 真在做 NLI 蕴含校验；
 * 说"最多两轮重规划"是因为 `agents/nodes/replanner.py` 里就是这么限的。
 * demo 区直接打真实后端，不是录屏。
 */
import {
  PhMagnifyingGlass,
  PhShieldCheck,
  PhStack,
  PhGraph,
  PhSelectionAll,
  PhLightning,
  PhArrowRight,
  PhArrowSquareOut,
  PhChatCircleDots,
  PhPaperPlaneRight,
} from '@phosphor-icons/vue'

definePageMeta({ layout: 'marketing' })

useSeoMeta({
  title: 'Research Copilot · 把一堆论文变成能对话的知识',
  description:
    '本地部署的多智能体学术研究工作台：PDF 混合检索、CRAG 三级降级、跨篇推理、引文图谱与写作用词，回答带句级溯源。',
  ogTitle: 'Research Copilot · 多智能体学术研究工作台',
  ogDescription: 'PDF → 双向量混合检索 → LangGraph 编排 → 带句级溯源的流式回答。',
  ogType: 'website',
})

/** 检索/编排管线的一行摘要，放在 Hero 下方当"技术名片"。 */
const pipeline = [
  { label: '解析入库', detail: 'MinerU / PyPDF' },
  { label: '双向量', detail: 'bge-m3 Dense+Sparse' },
  { label: '混合检索', detail: 'Milvus + RRF k=60' },
  { label: '智能体编排', detail: 'LangGraph 循环图' },
  { label: '溯源校验', detail: 'NLI 蕴含验证' },
] as const

const features = [
  {
    icon: PhMagnifyingGlass,
    title: '混合检索，不是关键词匹配',
    body: 'Dense 与 Sparse 双路召回后用 RRF（k=60，基于排名而非原始分）融合，再过 CrossEncoder 重排 —— 换个说法问同一个概念也能命中。',
  },
  {
    icon: PhShieldCheck,
    title: '答案必须带得出出处',
    body: 'Self-Citation 标注来源，NLI 蕴含校验逐条比对原文，Grounding Ratio 低于阈值直接不放行。未通过校验的引用角标会标红。',
  },
  {
    icon: PhStack,
    title: '检索不到就换个打法',
    body: 'CRAG 三级降级：相关 → 直接生成；含糊 → 改写查询重试（最多 3 次）；不相关 → 转网络检索兜底。不会硬着头皮编。',
  },
  {
    icon: PhLightning,
    title: '先规划，再执行，还会反思',
    body: '意图识别分 8 类，规划器出结构化计划，执行器逐步跑，反思器按 4 个维度打分，不达标就重做（最多两轮重规划）。',
  },
  {
    icon: PhGraph,
    title: '看得见的引文网络',
    body: '力导向图渲染论文节点与引用边，按度数 / PageRank 上色，缩放平移、点节点直接跳到原文。',
  },
  {
    icon: PhSelectionAll,
    title: '选中一段，就这段提问',
    body: '在 PDF 里划词即可追问。选区坐标存归一化值，所以放大缩小多少次，高亮框都不会飘。',
  },
] as const

const scenarios = [
  {
    who: '开题与文献综述',
    what: '把几十篇 PDF 丢进去，问"这一批工作在方法上分成几派、分歧点在哪"，而不是逐篇读摘要。',
  },
  {
    who: '写论文找依据',
    what: '写作辅助出结构化草稿，每条论断标出来自哪篇的哪一页，方便回溯核对。',
  },
  {
    who: '跨领域借鉴',
    what: '检索不到就自动转网络检索，把相邻领域的做法拉进来做类比。',
  },
  {
    who: '复现别人的方法',
    what: '对着原文划词追问细节，让数据在沙箱里跑一遍再回答，不用来回切窗口。',
  },
] as const

const faqs = [
  {
    q: '数据会传到外部吗？',
    a: '检索、向量库（Milvus）与结构化数据（PostgreSQL）都是本地服务，PDF 与切片不出本机。只有调用大模型这一步会按你配置的端点发出问题与检索到的片段 —— 换成自建或内网模型端点即可完全离线。',
  },
  {
    q: '和"跟 PDF 聊天"类工具有什么区别？',
    a: '区别在两点。一是它问的是一整个语料库而不是单个文件，能跨篇对比与推理；二是它的每个结论都要过溯源校验，引用不进检索上下文就不允许出现，宁可说"没找到"也不编。',
  },
  {
    q: '为什么强调"多智能体"而不是一个大模型？',
    a: '因为一次研究提问包含好几类不同工作：判断意图、拆解计划、检索、必要时调外部工具、检查自己的答案可不可信。拆成独立节点后，每一环都能单独约束与观测 —— 你能在界面上看到它到底查了什么、反思了几轮。',
  },
  {
    q: '支持哪些格式？',
    a: '目前以学术 PDF 为主，解析走 MinerU，本机没装 MinerU 时自动降级为 PyPDF。arXiv、PubMed、Semantic Scholar 与网络检索都封装成了独立工具，由智能体按需调用。',
  },
  {
    q: '怎么部署？',
    a: 'Docker Compose 一条命令拉起全部服务。首次启动会拉取 bge-m3 模型（约 2GB）到本地卷，之后重建镜像不会重复下载。',
  },
] as const
</script>

<template>
  <div>
    <!-- ─────────────── Hero ─────────────── -->
    <section class="mx-auto max-w-page px-5 pb-16 pt-14 sm:px-8 sm:pt-20">
      <MarketingReveal>
        <p class="inline-flex items-center gap-1.5 rounded-full bg-brand-soft px-3 py-1 text-[12px] text-brand-ink">
          <PhChatCircleDots :size="13" />
          本地部署 · 答必溯源
        </p>
      </MarketingReveal>

      <MarketingReveal :delay="60">
        <h1 class="mt-6 max-w-3xl text-display-l font-medium text-ink sm:text-display-xl">
          把一堆论文，变成<br class="hidden sm:block" />能对话的知识
        </h1>
      </MarketingReveal>

      <MarketingReveal :delay="120">
        <p class="mt-6 max-w-text text-lead text-ink-2">
          上传 PDF，它负责切分、双向量检索、跨篇推理，最后给出带句级溯源的回答。
          每一步都能看见 —— 包括它查了什么、反思了几轮、哪句话没能找到依据。
        </p>
      </MarketingReveal>

      <MarketingReveal :delay="180">
        <div class="mt-8 flex flex-wrap items-center gap-3">
          <NuxtLink
            to="/app"
            class="inline-flex items-center gap-1.5 rounded-lg bg-brand px-5 py-3 text-[14.5px] font-medium text-white transition-colors duration-[var(--dur-fast)] hover:bg-brand-ink"
          >
            进入工作台
            <PhArrowRight :size="15" weight="bold" />
          </NuxtLink>
          <a
            href="#demo"
            class="inline-flex items-center gap-1.5 rounded-lg border border-hairline bg-surface px-5 py-3 text-[14.5px] text-ink-2 transition-colors duration-[var(--dur-fast)] hover:bg-hover hover:text-ink"
          >
            看它现场跑一遍
            <PhPaperPlaneRight :size="15" />
          </a>
        </div>
      </MarketingReveal>

      <!-- 管线摘要 -->
      <MarketingReveal :delay="240">
        <ol class="mt-14 grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-hairline bg-hairline sm:grid-cols-3 lg:grid-cols-5">
          <li v-for="step in pipeline" :key="step.label" class="bg-surface px-4 py-4">
            <p class="text-[13px] font-medium text-ink">{{ step.label }}</p>
            <p class="mt-1 font-mono text-[11px] text-ink-4">{{ step.detail }}</p>
          </li>
        </ol>
      </MarketingReveal>
    </section>

    <!-- ─────────────── 功能 ─────────────── -->
    <section id="features" class="mx-auto max-w-page scroll-mt-20 px-5 py-20 sm:px-8">
      <MarketingReveal>
        <div class="mb-10 max-w-text">
          <h2 class="text-section font-medium text-ink">它不是"更快的搜索"，是更完整的推理链</h2>
          <p class="mt-3 text-lead text-ink-2">
            下面六条都对应具体的实现，不是宣传话术。
          </p>
        </div>
      </MarketingReveal>

      <div class="grid gap-px overflow-hidden rounded-xl border border-hairline bg-hairline sm:grid-cols-2 lg:grid-cols-3">
        <MarketingReveal
          v-for="(f, i) in features"
          :key="f.title"
          :delay="i * 60"
          class="bg-surface p-6 transition-colors duration-[var(--dur-fast)] hover:bg-hover"
        >
          <span class="inline-flex h-9 w-9 items-center justify-center rounded-lg bg-brand-soft text-brand-ink">
            <component :is="f.icon" :size="18" />
          </span>
          <h3 class="mt-4 text-[15px] font-medium text-ink">{{ f.title }}</h3>
          <p class="mt-2 text-[13.5px] leading-relaxed text-ink-2">{{ f.body }}</p>
        </MarketingReveal>
      </div>
    </section>

    <!-- ─────────────── 使用场景 ─────────────── -->
    <section class="mx-auto max-w-page px-5 py-16 sm:px-8">
      <MarketingReveal>
        <h2 class="text-section font-medium text-ink">谁在用它做什么</h2>
      </MarketingReveal>

      <div class="mt-8 grid gap-4 sm:grid-cols-2">
        <MarketingReveal
          v-for="(s, i) in scenarios"
          :key="s.who"
          :delay="i * 60"
          class="rounded-xl border border-hairline bg-surface p-5 transition-colors duration-[var(--dur-fast)] hover:border-brand-ring"
        >
          <p class="text-[14.5px] font-medium text-ink">{{ s.who }}</p>
          <p class="mt-2 text-[13.5px] leading-relaxed text-ink-2">{{ s.what }}</p>
        </MarketingReveal>
      </div>
    </section>

    <!-- ─────────────── 实时演示（SSE） ─────────────── -->
    <div class="py-16">
      <MarketingLiveDemo />
    </div>

    <!-- ─────────────── FAQ ─────────────── -->
    <section class="mx-auto max-w-page px-5 py-16 sm:px-8">
      <MarketingReveal>
        <h2 class="mb-8 text-section font-medium text-ink">常见问题</h2>
      </MarketingReveal>
      <MarketingReveal :delay="80">
        <div class="mx-auto max-w-3xl">
          <MarketingFaq :items="faqs" />
        </div>
      </MarketingReveal>
    </section>

    <!-- ─────────────── 结尾 CTA ─────────────── -->
    <section class="mx-auto max-w-page px-5 pb-8 sm:px-8">
      <MarketingReveal>
        <div class="rounded-2xl border border-hairline bg-surface px-6 py-12 text-center sm:px-12">
          <h2 class="text-display-m font-medium text-ink">打开就能问</h2>
          <p class="mx-auto mt-3 max-w-text text-lead text-ink-2">
            工作台已经跑在本地了。上传第一篇 PDF，剩下的交给它。
          </p>
          <div class="mt-7 flex flex-wrap justify-center gap-3">
            <NuxtLink
              to="/app"
              class="inline-flex items-center gap-1.5 rounded-lg bg-brand px-5 py-3 text-[14.5px] font-medium text-white transition-colors duration-[var(--dur-fast)] hover:bg-brand-ink"
            >
              进入工作台
              <PhArrowRight :size="15" weight="bold" />
            </NuxtLink>
            <NuxtLink
              to="/about"
              class="inline-flex items-center gap-1.5 rounded-lg border border-hairline px-5 py-3 text-[14.5px] text-ink-2 transition-colors duration-[var(--dur-fast)] hover:bg-hover hover:text-ink"
            >
              看看它是怎么搭的
              <PhArrowSquareOut :size="15" />
            </NuxtLink>
          </div>
        </div>
      </MarketingReveal>
    </section>
  </div>
</template>
