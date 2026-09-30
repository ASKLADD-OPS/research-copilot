<script setup lang="ts">
/**
 * 全站错误页 —— 只负责**真正的异常**（5xx / 页面抛错）。
 *
 * 404 不走这里，走 `pages/[...slug].vue`（原因见 `components/ErrorView.vue` 顶部注释：
 * 走错误管线会把空原型的 NuxtError 塞进 payload，撞上 @pinia/nuxt 的 payload reducer）。
 *
 * error.vue 拿不到 layout，所以页头要自己拼。
 */
import type { NuxtError } from '#app'

const props = defineProps<{ error: NuxtError }>()

const statusCode = computed(() => props.error?.statusCode ?? 500)

/** 用 redirect 而不是裸 clearError：否则清掉错误后仍停在那个坏路径上，会立刻再触发一次。 */
function goHome() {
  return clearError({ redirect: '/' })
}
</script>

<template>
  <div class="flex min-h-screen flex-col bg-canvas">
    <header class="px-5 py-6 sm:px-8">
      <button type="button" aria-label="返回首页" @click="goHome">
        <MarketingLogo />
      </button>
    </header>

    <main class="flex-1">
      <ErrorView :status-code="statusCode" :message="error?.message" />
    </main>
  </div>
</template>
