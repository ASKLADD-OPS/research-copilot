<script setup lang="ts">
/**
 * 滚动入场。包装任意内容，进入视口时淡入上移一次。
 *
 * 为什么用 IntersectionObserver 而不是监听 scroll：
 * scroll 事件里读 getBoundingClientRect 会触发强制同步布局，一屏十几个元素就掉帧。
 *
 * 三条必须处理的边界：
 *  1. `prefers-reduced-motion` —— 直接置为已显示，不走过渡（CSS 变量同时也归零了，双保险）。
 *  2. 老浏览器没有 IntersectionObserver —— 直接显示，不能把内容永久藏起来。
 *  3. 显示后就 `disconnect` —— 否则用户来回滚动会反复触发，视觉上像闪烁。
 *
 * 已知取舍：内容默认 `opacity-0`，所以**禁用 JS 时不可见**（但文本仍在 HTML 里，可被爬虫读到）。
 * 对一个要 hydration 的 Nuxt 应用来说这可以接受 —— 换来的是不会"先闪一下再动"。
 */
import type { CSSProperties } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 同组元素错开出场，单位 ms。整组请用 0/80/160 这样的等差数列。 */
    delay?: number
    /** 入场时上移的距离（px）。 */
    offset?: number
  }>(),
  { delay: 0, offset: 12 },
)

const el = ref<HTMLElement | null>(null)
const shown = ref(false)
let io: IntersectionObserver | null = null

const style = computed<CSSProperties>(() => ({
  transitionDelay: `${props.delay}ms`,
  '--reveal-offset': `${props.offset}px`,
}))

onMounted(() => {
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  if (reduce || typeof IntersectionObserver === 'undefined') {
    shown.value = true
    return
  }

  io = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue
        shown.value = true
        io?.disconnect()
        io = null
        return
      }
    },
    // 底部提前 12% 触发：等元素完全进视口才动，用户会先看到一段静态
    { rootMargin: '0px 0px -12% 0px', threshold: 0.08 },
  )
  if (el.value) io.observe(el.value)
})

onBeforeUnmount(() => {
  io?.disconnect()
  io = null
})
</script>

<template>
  <div
    ref="el"
    class="reveal"
    :class="shown ? 'is-shown' : ''"
    :style="style"
  >
    <slot />
  </div>
</template>

<style scoped>
.reveal {
  opacity: 0;
  transform: translateY(var(--reveal-offset, 12px));
  transition:
    opacity var(--dur-reveal) var(--ease-out-soft),
    transform var(--dur-reveal) var(--ease-out-soft);
  will-change: opacity, transform;
}

.reveal.is-shown {
  opacity: 1;
  transform: none;
  /* 动画结束后撤掉合成层提示，否则几十个元素会一直占显存 */
  will-change: auto;
}
</style>
