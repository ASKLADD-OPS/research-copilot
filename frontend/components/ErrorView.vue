<script setup lang="ts">
/**
 * 错误卡片。被两处共用：
 *   - `pages/[...slug].vue` —— 兜底路由，接管 404
 *   - `error.vue`            —— 真正的异常（5xx）
 *
 * 为什么要拆出兜底路由，而不是只用 `error.vue`：
 * 本项目踩到一个 Nuxt × Pinia 的兼容问题 —— `@pinia/nuxt` 注册的 payload reducer 会遍历
 * payload 里**每一个值**，而它内部用的是 `obj.hasOwnProperty(...)`；偏偏 Nuxt 的
 * `createError()` 产出的错误对象是**空原型**（`Object.getPrototypeOf(err) === null`），
 * 没有 `hasOwnProperty`，于是序列化 payload 时抛
 * `TypeError: obj.hasOwnProperty is not a function`，把 404 变成了 500。
 *
 * 未匹配路由走兜底页后就不再产生错误对象，问题绕开了。真正抛异常的 5xx 仍会走 `error.vue`，
 * 那一条受上游版本影响，已在交付说明里单列。
 */
import { PhArrowLeft, PhWarningCircle, PhMagnifyingGlass } from '@phosphor-icons/vue'

const props = withDefaults(
  defineProps<{
    statusCode?: number
    message?: string
  }>(),
  { statusCode: 404 },
)

const isNotFound = computed(() => props.statusCode === 404)

const title = computed(() => (isNotFound.value ? '这个页面不存在' : '出了点问题'))

const detail = computed(() =>
  isNotFound.value
    ? '地址可能拼错了，或者这个页面还没做。首页、定价、关于、工作台都是通的。'
    : props.message || '服务端返回了未预期的错误。',
)

function goBack() {
  if (import.meta.client && window.history.length > 1) {
    window.history.back()
    return
  }
  return navigateTo('/')
}
</script>

<template>
  <div class="mx-auto max-w-page px-5 py-20 sm:px-8">
    <div class="mx-auto max-w-md rounded-xl border border-hairline bg-surface p-7 shadow-sm">
      <div class="flex items-center gap-3">
        <span class="inline-flex h-9 w-9 items-center justify-center rounded-lg bg-bad-soft text-bad">
          <PhMagnifyingGlass v-if="isNotFound" :size="18" />
          <PhWarningCircle v-else :size="18" />
        </span>
        <div>
          <h1 class="text-[17px] font-medium text-ink">{{ title }}</h1>
          <p class="font-mono text-[12px] text-ink-4">HTTP {{ statusCode }}</p>
        </div>
      </div>

      <p class="mt-4 text-[13.5px] leading-relaxed text-ink-2">{{ detail }}</p>

      <div class="mt-6 flex flex-wrap gap-2">
        <NuxtLink
          to="/"
          class="inline-flex items-center gap-1.5 rounded-lg bg-brand px-4 py-2 text-[13.5px] font-medium text-white transition-colors duration-[var(--dur-fast)] hover:bg-brand-ink"
        >
          <PhArrowLeft :size="14" weight="bold" />
          回到首页
        </NuxtLink>
        <button
          type="button"
          class="rounded-lg border border-hairline px-4 py-2 text-[13.5px] text-ink-2 transition-colors duration-[var(--dur-fast)] hover:bg-hover"
          @click="goBack"
        >
          返回上一页
        </button>
        <NuxtLink
          to="/app"
          class="rounded-lg border border-hairline px-4 py-2 text-[13.5px] text-ink-2 transition-colors duration-[var(--dur-fast)] hover:bg-hover"
        >
          进入工作台
        </NuxtLink>
      </div>
    </div>
  </div>
</template>
