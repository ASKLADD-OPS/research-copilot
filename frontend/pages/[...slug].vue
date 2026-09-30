<script setup lang="ts">
/**
 * 兜底路由：接管所有未匹配的路径，渲染 404。
 *
 * 为什么不用（只用）`error.vue`：见 `components/ErrorView.vue` 顶部的说明 ——
 * 走 Nuxt 的错误管线会把 NuxtError 对象塞进 payload，而那是空原型对象，
 * 会撞上 `@pinia/nuxt` 的 payload reducer（它用 `obj.hasOwnProperty`），
 * 结果是 404 变成 500。用兜底页渲染就完全不产生错误对象，绕开了这个上游兼容问题。
 *
 * 放在 `pages/` 下还有个附带好处：它能套 `marketing` layout，
 * 所以 404 页也有完整的页头页脚 —— 而 `error.vue` 拿不到 layout。
 */
definePageMeta({ layout: 'marketing' })

// 必须是真 404：否则搜索引擎会把不存在的路径当成正常页面收录
setResponseStatus(404)

useSeoMeta({
  title: '页面不存在 · Research Copilot',
  robots: 'noindex, nofollow',
})
</script>

<template>
  <ErrorView :status-code="404" />
</template>
