<script setup lang="ts">
/**
 * 定价页。
 *
 * 刻意**不接支付**：本项目是本地部署的科研工具，没有在线订阅这条业务线。
 * 参考站的 /pricing 背后有 /subscription 与 /payment-result，
 * 那涉及对账、退款与合规，不属于"改 UI"能覆盖的范围。
 *
 * 所以这里的"定价"讲的是**部署方式与相应工作量**，并且明确标出哪些能力还没做 ——
 * 把没实现的东西写成权益是最容易失去用户信任的做法。
 */
import { PhCheck, PhArrowRight, PhInfo } from '@phosphor-icons/vue'

definePageMeta({ layout: 'marketing' })

useSeoMeta({
  title: '部署与定价 · Research Copilot',
  description: '本地部署方案：单机自持、实验室主机共享、定制集成。没有在线订阅，数据不出本机。',
})

interface Plan {
  name: string
  price: string
  note: string
  desc: string
  features: string[]
  cta: string
  to: string
  highlight?: boolean
}

const plans: Plan[] = [
  {
    name: '单机自持',
    price: '现在就能用',
    note: 'Docker Compose 一条命令拉起',
    desc: '一台机器跑完检索、向量库与编排。适合个人研究者。',
    features: [
      '文献库 / 阅读器 / 引文图谱 / 写作 / 工具台全套',
      'PDF 混合检索 + CRAG 三级降级',
      '句级溯源与引用校验',
      '外部检索：arXiv / PubMed / Semantic Scholar',
      '数据全部留在本机',
    ],
    cta: '进入工作台',
    to: '/app',
    highlight: true,
  },
  {
    name: '实验室主机',
    price: '需要额外开发',
    note: '账号体系尚未实现',
    desc: '把同一套服务部署到实验室服务器，组内通过内网访问。',
    features: [
      '继承单机版的全部能力',
      '集中式 PostgreSQL 与 Milvus',
      '统一模型端点与配额管理',
      '⚠ 多用户账号与权限：后端目前没有任何登录接口，',
      '　 需先补 User 模型、/auth 路由与前端路由守卫',
    ],
    cta: '聊聊需求',
    to: '/about#stack',
  },
  {
    name: '定制集成',
    price: '按需',
    note: '接你自己的模型与数据源',
    desc: '把模型换成自建端点，或扩充检索数据源与工具。',
    features: [
      '任意 OpenAI 兼容端点（本地 vLLM / 内网网关均可）',
      '新增 MCP 工具（工具协议已统一封装）',
      '自定义解析链路与切分策略',
      '内网离线部署',
    ],
    cta: '看看架构成不成',
    to: '/about#stack',
  },
]

const included = [
  '没有在线订阅，也不需要账号',
  '模型调用只按你自己的端点计费',
  '文献、切片与向量不出本机',
  '全部源码可读，可自行修改',
] as const
</script>

<template>
  <div>
    <section class="mx-auto max-w-page px-5 pb-12 pt-14 sm:px-8 sm:pt-20">
      <MarketingReveal>
        <h1 class="max-w-3xl text-display-l font-medium text-ink">没有订阅，只有部署方式</h1>
        <p class="mt-6 max-w-text text-lead text-ink-2">
          这是个跑在你自己机器上的工具，所以这里不卖席位。下面是三种部署形态，
          以及各自<strong class="font-medium text-ink">还需要补什么</strong>。
        </p>
      </MarketingReveal>
    </section>

    <section class="mx-auto max-w-page px-5 pb-16 sm:px-8">
      <div class="grid gap-4 lg:grid-cols-3">
        <MarketingReveal
          v-for="(plan, i) in plans"
          :key="plan.name"
          :delay="i * 80"
          class="flex flex-col rounded-xl border bg-surface p-6"
          :class="plan.highlight ? 'border-brand-ring shadow-sm' : 'border-hairline'"
        >
          <div class="flex items-center justify-between gap-3">
            <h2 class="text-[15px] font-medium text-ink">{{ plan.name }}</h2>
            <span
              v-if="plan.highlight"
              class="rounded-full bg-brand-soft px-2.5 py-0.5 text-[11px] text-brand-ink"
            >
              推荐
            </span>
          </div>

          <p class="mt-4 text-display-m font-medium text-ink">{{ plan.price }}</p>
          <p class="mt-1 text-[12.5px] text-ink-3">{{ plan.note }}</p>

          <p class="mt-4 text-[13.5px] leading-relaxed text-ink-2">{{ plan.desc }}</p>

          <ul class="mt-6 flex-1 space-y-2.5">
            <li v-for="f in plan.features" :key="f" class="flex gap-2 text-[13px] leading-relaxed text-ink-2">
              <PhCheck
                v-if="!f.startsWith('⚠') && !f.startsWith('　')"
                :size="14"
                weight="bold"
                class="mt-[3px] shrink-0 text-ok"
              />
              <span
                v-else
                class="whitespace-pre-wrap"
                :class="f.startsWith('⚠') ? 'text-warn' : 'pl-[22px] text-ink-3'"
              >{{ f }}</span>
            </li>
          </ul>

          <NuxtLink
            :to="plan.to"
            class="mt-7 inline-flex items-center justify-center gap-1.5 rounded-lg px-4 py-2.5 text-[13.5px] font-medium transition-colors duration-[var(--dur-fast)]"
            :class="
              plan.highlight
                ? 'bg-brand text-white hover:bg-brand-ink'
                : 'border border-hairline text-ink-2 hover:bg-hover hover:text-ink'
            "
          >
            {{ plan.cta }}
            <PhArrowRight :size="14" weight="bold" />
          </NuxtLink>
        </MarketingReveal>
      </div>

      <MarketingReveal :delay="240">
        <div class="mt-10 rounded-xl border border-hairline bg-surface p-5">
          <h2 class="flex items-center gap-2 text-[14px] font-medium text-ink">
            <PhInfo :size="15" class="text-ink-3" />
            三档共通
          </h2>
          <ul class="mt-4 grid gap-2.5 sm:grid-cols-2">
            <li v-for="item in included" :key="item" class="flex gap-2 text-[13px] text-ink-2">
              <PhCheck :size="14" weight="bold" class="mt-[3px] shrink-0 text-ok" />
              {{ item }}
            </li>
          </ul>
        </div>
      </MarketingReveal>
    </section>
  </div>
</template>
