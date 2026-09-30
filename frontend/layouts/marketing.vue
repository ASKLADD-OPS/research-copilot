<script setup lang="ts">
/**
 * 营销站外壳：吸顶导航 + 内容 + 页脚。
 *
 * 刻意**不建** `layouts/default.vue`：工作台 `/app` 自己就是壳，
 * 如果存在 default layout，它会被自动套上营销导航 —— 那正是要避免的。
 * 所以这里用命名 layout，由营销页通过 `definePageMeta({ layout: 'marketing' })` 显式选用。
 */
</script>

<template>
  <div class="flex min-h-screen flex-col bg-canvas">
    <!-- 跳转链接：键盘用户不必逐条 Tab 过导航才能到正文 -->
    <a
      href="#main"
      class="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:border focus:border-hairline focus:bg-surface focus:px-4 focus:py-2 focus:text-[13px] focus:text-ink focus:shadow-md"
    >
      跳到主要内容
    </a>

    <MarketingHeader />

    <main id="main" class="flex-1">
      <slot />
    </main>

    <MarketingFooter />
  </div>
</template>
