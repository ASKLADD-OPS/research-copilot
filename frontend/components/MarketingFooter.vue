<script setup lang="ts">
/**
 * 营销站页脚。
 *
 * 只放**真实存在**的页面链接 —— 参考站那种"博客/帮助/更新日志"我们还没有，
 * 先不列，避免点进去 404。等阶段 5 博客上线后再补。
 */
const year = new Date().getFullYear()

const groups = [
  {
    title: '产品',
    links: [
      { label: '功能总览', to: '/#features' },
      { label: '定价', to: '/pricing' },
      { label: '进入工作台', to: '/app' },
    ],
  },
  {
    title: '项目',
    links: [
      { label: '关于', to: '/about' },
      { label: '技术架构', to: '/about#stack' },
    ],
  },
] as const
</script>

<template>
  <footer class="mt-24 border-t border-hairline bg-surface">
    <div class="mx-auto max-w-page px-5 py-14 sm:px-8">
      <div class="flex flex-col gap-10 sm:flex-row sm:justify-between">
        <div class="max-w-xs">
          <MarketingLogo />
          <p class="mt-4 text-[13px] leading-relaxed text-ink-3">
            面向科研语料的多智能体研究助手：检索 · 问答 · 引文图谱 · 写作，答必溯源。
          </p>
        </div>

        <div class="flex gap-16">
          <div v-for="group in groups" :key="group.title">
            <h3 class="text-[12px] font-medium uppercase tracking-wide text-ink-4">
              {{ group.title }}
            </h3>
            <ul class="mt-3 space-y-2.5">
              <li v-for="link in group.links" :key="link.to">
                <NuxtLink
                  :to="link.to"
                  class="text-[13.5px] text-ink-2 transition-colors duration-[var(--dur-fast)] hover:text-brand-ink"
                >
                  {{ link.label }}
                </NuxtLink>
              </li>
            </ul>
          </div>
        </div>
      </div>

      <div class="mt-12 flex flex-col gap-2 border-t border-hairline pt-6 text-[12px] text-ink-4 sm:flex-row sm:items-center sm:justify-between">
        <p>© {{ year }} Research Copilot · 本地部署的科研工作台</p>
        <p>所有回答均标注来源段落，不做无依据引用</p>
      </div>
    </div>
  </footer>
</template>
