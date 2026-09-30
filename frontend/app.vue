<script setup lang="ts">
/**
 * 应用根。
 *
 * 这里**必须**包 `<NuxtLayout>`：页面通过 `definePageMeta({ layout: 'marketing' })`
 * 指定的命名 layout 只有经过 `<NuxtLayout>` 才会被解析。少了它，Nuxt 会静默忽略
 * layout 声明 —— 页面照常渲染，但吸顶导航、页脚、跳转链接一个都不出现，且不报任何错。
 *
 * `/app`（工作台）自己就是壳，用 `layout: false` 显式退出；项目里也刻意**不建**
 * `layouts/default.vue`，这样没有声明 layout 的页面同样是裸的，不会被营销导航套上。
 */
useHead({ htmlAttrs: { lang: 'zh-CN' } })
</script>

<template>
  <NuxtLayout>
    <NuxtPage />
  </NuxtLayout>
</template>
