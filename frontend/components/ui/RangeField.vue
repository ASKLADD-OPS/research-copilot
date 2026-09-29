<script setup lang="ts">
/**
 * 带标签与数值回显的滑杆。图谱参数面板里有 7 个同构的行 —— 这正是值得抽组件的信号：
 * 同样的结构重复 7 遍，任何一处想加"双击重置"都得改 7 个地方。
 *
 * 原生 `<input type="range">` 够用：Tailwind 的 `accent-*` 能直接改滑块与轨道色，
 * 为它引一个滑块组件库不划算。
 */
const props = withDefaults(
  defineProps<{
    label: string
    modelValue: number
    min: number
    max: number
    step?: number
    /** 数值后缀，如 px / % */
    suffix?: string
    /** 提示：这个参数做什么的 */
    hint?: string
  }>(),
  { step: 1, suffix: '', hint: '' },
)

const emit = defineEmits<{ 'update:modelValue': [value: number] }>()

function onInput(e: Event) {
  emit('update:modelValue', Number((e.target as HTMLInputElement).value))
}

const shown = computed(() => {
  const v = props.modelValue
  const text = props.step < 1 ? v.toFixed(2).replace(/0+$/, '').replace(/\.$/, '') : String(v)
  return `${text}${props.suffix}`
})
</script>

<template>
  <label class="block" :title="hint">
    <span class="flex items-center gap-2">
      <span :class="LABEL_CLS" class="flex-1">{{ label }}</span>
      <span class="text-2xs tabular-nums text-ink-2">{{ shown }}</span>
    </span>
    <input
      class="mt-1 h-1 w-full cursor-pointer appearance-none rounded-full bg-sunken accent-brand"
      type="range"
      :min="min"
      :max="max"
      :step="step"
      :value="modelValue"
      @input="onInput"
    />
  </label>
</template>
