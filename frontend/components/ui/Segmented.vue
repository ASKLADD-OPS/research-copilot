<script setup lang="ts" generic="T extends string">
/**
 * 分段控件。用泛型而不是 `string`：`v-model` 绑到一个联合类型（如 MainView）时，
 * 泛型能把"传进去的选项值必须是 MainView"这件事在校验期卡住，
 * 否则拼错一个字符串要到运行时点了没反应才发现。
 */
import type { Component } from 'vue'

withDefaults(
  defineProps<{
    modelValue: T
    options: ReadonlyArray<{ value: T; label: string; icon?: Component }>
    size?: 'sm' | 'md'
    ariaLabel?: string
  }>(),
  { size: 'md', ariaLabel: '视图切换' },
)

const emit = defineEmits<{ 'update:modelValue': [value: T] }>()
</script>

<template>
  <div
    class="inline-flex shrink-0 items-center gap-0.5 rounded-lg bg-sunken p-0.5"
    role="tablist"
    :aria-label="ariaLabel"
  >
    <button
      v-for="opt in options"
      :key="opt.value"
      type="button"
      role="tab"
      :aria-selected="opt.value === modelValue"
      class="inline-flex items-center gap-1.5 rounded-md font-medium whitespace-nowrap transition-colors"
      :class="[
        size === 'sm' ? 'h-6 px-2 text-[11.5px]' : 'h-6.5 px-2.5 text-[12px]',
        opt.value === modelValue
          ? 'bg-surface text-ink shadow-xs'
          : 'text-ink-3 hover:text-ink',
      ]"
      @click="emit('update:modelValue', opt.value)"
    >
      <component :is="opt.icon" v-if="opt.icon" :size="12" />
      {{ opt.label }}
    </button>
  </div>
</template>
