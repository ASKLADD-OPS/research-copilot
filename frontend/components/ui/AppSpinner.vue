<script setup lang="ts">
/**
 * 转圈。抽出来只因为它在 8 个地方出现 —— 内联 8 遍 `animate-spin` 加一个图标名，
 * 改一次要搜 8 遍。
 */
import { PhCircleNotch } from '@phosphor-icons/vue'

withDefaults(defineProps<{ size?: number; class?: string }>(), { size: 13, class: '' })
</script>

<template>
  <PhCircleNotch :size="size" class="shrink-0 animate-spin text-current" aria-hidden="true" />
</template>
