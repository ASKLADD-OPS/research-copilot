<script setup lang="ts">
/** 分段控件（选项组）。不是"开关" —— 开关用 .rc-switch。 */
defineProps<{
  modelValue: string
  options: ReadonlyArray<{ value: string; label: string }>
}>()

const emit = defineEmits<{ 'update:modelValue': [string] }>()
</script>

<template>
  <div class="rc-seg" role="group">
    <button
      v-for="opt in options"
      :key="opt.value"
      type="button"
      :aria-pressed="opt.value === modelValue"
      @click="emit('update:modelValue', opt.value)"
    >
      {{ opt.label }}
    </button>
  </div>
</template>
