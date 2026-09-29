<script setup lang="ts">
// 应用根。外壳（顶栏 / 三栏 / 状态条）由 pages/index.vue 自己组装 —— 这个项目只有
// 一个页面，为它套一层 Layout 只是多一次组件跳转，不换来任何东西。
useHead({ htmlAttrs: { lang: 'zh-CN' } })
</script>

<template>
  <NuxtPage />
</template>
