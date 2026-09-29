<script setup lang="ts">
// 全局初始化：检查后端健康状态，供 AppShell 的状态灯使用
const ui = useUiStore()
onMounted(() => void ui.refreshHealth())
</script>

<template>
  <AppShell>
    <NuxtPage />
  </AppShell>
</template>
