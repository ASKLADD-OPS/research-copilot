<script setup lang="ts">
/**
 * 关于页。
 *
 * 结构：定位 → 架构分层 → 技术栈明细（#stack）→ 明确不做的事。
 * 最后一段是刻意的：把"不做什么"写清楚，比堆功能更能说明产品边界。
 */
import { PhArrowRight } from '@phosphor-icons/vue'

definePageMeta({ layout: 'marketing' })

useSeoMeta({
  title: '关于 · Research Copilot',
  description:
    'Research Copilot 的定位、架构分层与技术栈：FastAPI + LangGraph + Milvus + PostgreSQL，外部工具统一走 MCP。',
})

const layers = [
  {
    name: '检索层',
    items: ['MinerU / PyPDF 解析', '结构感知切分', 'bge-m3 双向量（Dense + Sparse）', 'Milvus 混合检索 + RRF(k=60) + CrossEncoder 重排'],
  },
  {
    name: '编排层',
    items: ['LangGraph 有向循环图', '意图识别（8 类）+ 澄清追问', 'ReAct 执行循环', 'Plan-and-Execute + 最多两轮重规划', 'Reflection 四维打分'],
  },
  {
    name: '可信层',
    items: ['CRAG 三级降级', 'Self-Citation 标注', 'NLI 蕴含校验', 'Grounding Ratio 阈值拦截'],
  },
  {
    name: '工具层',
    items: ['arXiv / PubMed / Semantic Scholar', 'Python 沙箱执行', '网络检索兜底', '全部封装为独立 MCP Server'],
  },
] as const

const stack = [
  { k: '后端', v: 'Python 3.12 · FastAPI · SQLAlchemy 2.0 · Alembic' },
  { k: '编排', v: 'LangChain · LangGraph（StateGraph + Checkpointer）' },
  { k: '检索', v: 'Milvus 2.4 · bge-m3（ModelScope 加载）' },
  { k: '存储', v: 'PostgreSQL 16' },
  { k: '工具协议', v: 'FastMCP · fastapi-mcp · langchain-mcp-adapters' },
  { k: '前端', v: 'Nuxt 3 · Vue 3 · TypeScript · Pinia · Tailwind CSS v4' },
  { k: '阅读与图谱', v: 'pdfjs-dist · ECharts 力导向图' },
  { k: '部署', v: 'Docker Compose（长任务在进程内后台线程，不依赖 Celery）' },
] as const

const notDoing = [
  '不做通用聊天机器人 —— 它的答案必须能回指到你的语料',
  '不做"什么都答" —— 检索不到就走降级或转外部检索，不硬编',
  '不把数据传到我们这边 —— 没有我们的服务器',
  '不做在线订阅与支付 —— 部署即拥有',
] as const
</script>

<template>
  <div>
    <section class="mx-auto max-w-page px-5 pb-14 pt-14 sm:px-8 sm:pt-20">
      <MarketingReveal>
        <h1 class="max-w-3xl text-display-l font-medium text-ink">为什么要有这个工具</h1>
      </MarketingReveal>

      <MarketingReveal :delay="60">
        <div class="mt-8 grid gap-8 lg:grid-cols-2">
          <div class="space-y-4 text-lead text-ink-2">
            <p>
              读文献真正的瓶颈从来不是"找不到"，而是"读不完、串不起来"。
              一篇 PDF 读完 20 分钟，五十篇就是两周，而其中大部分内容对当前这个问题是无用的。
            </p>
            <p>
              更麻烦的是第二层问题：把结论交给一个语言模型，它会给你一段读起来很顺、
              但可能没有任何依据的答案。在科研场景里，<span class="text-ink">没有出处的结论等于零</span>。
            </p>
          </div>
          <div class="space-y-4 text-lead text-ink-2">
            <p>
              所以这个项目的两个设计前提是：
              <span class="text-ink">跨篇提问</span>（问的是语料，不是单个文件），
              <span class="text-ink">答必溯源</span>（引用不进检索上下文就不允许出现）。
            </p>
            <p>
              第二个前提决定了整个架构 —— 之所以要多智能体、要有反思节点、
              要有蕴含校验，都是为了让它在该说"我没找到依据"的时候真的说得出口。
            </p>
          </div>
        </div>
      </MarketingReveal>
    </section>

    <!-- 架构分层 -->
    <section class="mx-auto max-w-page px-5 py-14 sm:px-8">
      <MarketingReveal>
        <h2 class="text-section font-medium text-ink">四层结构</h2>
      </MarketingReveal>

      <div class="mt-8 grid gap-px overflow-hidden rounded-xl border border-hairline bg-hairline sm:grid-cols-2">
        <MarketingReveal
          v-for="(layer, i) in layers"
          :key="layer.name"
          :delay="i * 60"
          class="bg-surface p-5"
        >
          <h3 class="flex items-center gap-2 text-[14.5px] font-medium text-ink">
            <span class="inline-flex h-5 w-5 items-center justify-center rounded bg-brand-soft font-mono text-[11px] text-brand-ink">
              {{ i + 1 }}
            </span>
            {{ layer.name }}
          </h3>
          <ul class="mt-3 space-y-1.5">
            <li v-for="item in layer.items" :key="item" class="text-[13px] leading-relaxed text-ink-2">
              {{ item }}
            </li>
          </ul>
        </MarketingReveal>
      </div>
    </section>

    <!-- 技术栈（外卖链接指向这里） -->
    <section id="stack" class="mx-auto max-w-page scroll-mt-20 px-5 py-14 sm:px-8">
      <MarketingReveal>
        <h2 class="text-section font-medium text-ink">技术栈</h2>
      </MarketingReveal>

      <MarketingReveal :delay="80">
        <dl class="mt-8 divide-y divide-hairline overflow-hidden rounded-xl border border-hairline bg-surface">
          <div v-for="row in stack" :key="row.k" class="flex flex-col gap-1 px-5 py-4 sm:flex-row sm:items-baseline sm:gap-6">
            <dt class="w-28 shrink-0 text-[13px] text-ink-3">{{ row.k }}</dt>
            <dd class="font-mono text-[12.5px] leading-relaxed text-ink-2">{{ row.v }}</dd>
          </div>
        </dl>
      </MarketingReveal>
    </section>

    <!-- 不做的事 -->
    <section class="mx-auto max-w-page px-5 py-14 sm:px-8">
      <MarketingReveal>
        <h2 class="text-section font-medium text-ink">明确不做的事</h2>
      </MarketingReveal>

      <MarketingReveal :delay="80">
        <ul class="mt-6 grid gap-3 sm:grid-cols-2">
          <li
            v-for="item in notDoing"
            :key="item"
            class="rounded-xl border border-hairline bg-surface px-5 py-4 text-[13.5px] leading-relaxed text-ink-2"
          >
            {{ item }}
          </li>
        </ul>
      </MarketingReveal>

      <MarketingReveal :delay="160">
        <div class="mt-10">
          <NuxtLink
            to="/app"
            class="inline-flex items-center gap-1.5 rounded-lg bg-brand px-5 py-3 text-[14.5px] font-medium text-white transition-colors duration-[var(--dur-fast)] hover:bg-brand-ink"
          >
            去工作台试试
            <PhArrowRight :size="15" weight="bold" />
          </NuxtLink>
        </div>
      </MarketingReveal>
    </section>
  </div>
</template>
