<script setup lang="ts">
/**
 * 品牌标。
 *
 * 注意：项目目前**没有任何品牌资产**（无 public/、无 logo 文件），所以这里是自绘的
 * 几何标记，不是正式 logo。换正式 logo 时只改本文件的 <svg>，其余地方都引用本组件。
 *
 * 图形语义：三个节点 + 两条边，取自"引文图谱"这个产品核心物。
 */
withDefaults(
  defineProps<{
    /** 标记边长（px）。字标大小随之等比缩放。 */
    size?: number
    /** 只出图形不出文字（窄屏 / 折叠态用）。 */
    markOnly?: boolean
  }>(),
  { size: 28, markOnly: false },
)
</script>

<template>
  <span class="inline-flex select-none items-center gap-2">
    <svg
      :width="size"
      :height="size"
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden="true"
      class="shrink-0"
    >
      <rect width="32" height="32" rx="9" fill="var(--color-brand)" />
      <path
        d="M11.5 11.5 L21.5 15.5 M11.5 11.5 L13.5 21.5"
        stroke="#ffffff"
        stroke-width="1.7"
        stroke-linecap="round"
      />
      <circle cx="11.5" cy="11.5" r="3" fill="#ffffff" />
      <circle cx="21.5" cy="15.5" r="2.3" fill="#ffffff" />
      <circle cx="13.5" cy="21.5" r="2.3" fill="#ffffff" />
    </svg>

    <span v-if="!markOnly" class="flex flex-col leading-none">
      <span class="text-[15px] font-semibold tracking-tight text-ink">Research Copilot</span>
      <span class="mt-0.5 text-[11px] text-ink-3">多智能体学术研究工作台</span>
    </span>
  </span>
</template>
