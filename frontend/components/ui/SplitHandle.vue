<script setup lang="ts">
/**
 * 可拖拽分割线。
 *
 * 用 Pointer Events 而不是 mouse/touch 两套：pointerdown 自带 pointer capture，
 * 拖到 iframe 或面板外面也不会丢事件 —— 这是拖拽类交互最常见的 bug 来源。
 *
 * 键盘可达：聚焦后按 ←/→ 微调 16px，Enter/Space 折叠。拖拽是鼠标操作，
 * 但"调面板宽度"这件事键盘用户也得能做到，成本只有几行。
 */
import { PhCaretDoubleLeft, PhCaretDoubleRight } from '@phosphor-icons/vue'

const props = withDefaults(
  defineProps<{
    /** 相邻面板的当前宽度（px） */
    modelValue: number
    min?: number
    max?: number
    /** 面板在这一侧的哪边：left 表示面板在分割线左方，向右拖变大 */
    side?: 'left' | 'right'
    collapsed?: boolean
    /** 无障碍标签要说明它控的是什么，不能只写"分割线" */
    label: string
    /** 键盘微调的步长 */
    step?: number
  }>(),
  { min: 160, max: 720, side: 'left', collapsed: false, step: 16 },
)

const emit = defineEmits<{
  'update:modelValue': [value: number]
  toggle: []
}>()

const dragging = ref(false)

let startX = 0
let startSize = 0
let el: HTMLElement | null = null

function clamp(v: number) {
  return Math.min(props.max, Math.max(props.min, Math.round(v)))
}

function onPointerDown(e: PointerEvent) {
  // 折叠状态下不拖：此时"面板宽度"没有意义，拖它只会让尺寸和视觉对不上
  if (props.collapsed || e.button !== 0) return
  el = e.currentTarget as HTMLElement
  dragging.value = true
  startX = e.clientX
  startSize = props.modelValue
  el.setPointerCapture(e.pointerId)
  // 拖拽期间全局锁光标与选区，否则文字会被一路选中
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
}

function onPointerMove(e: PointerEvent) {
  if (!dragging.value) return
  const dx = e.clientX - startX
  emit('update:modelValue', clamp(props.side === 'left' ? startSize + dx : startSize - dx))
}

function onPointerUp(e: PointerEvent) {
  if (!dragging.value) return
  dragging.value = false
  el?.releasePointerCapture?.(e.pointerId)
  el = null
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    emit('toggle')
    return
  }
  const dir = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0
  if (!dir || props.collapsed) return
  e.preventDefault()
  // 向右拖 = 面板变大（面板在左），面板在右时方向反过来
  const sign = props.side === 'left' ? dir : -dir
  emit('update:modelValue', clamp(props.modelValue + sign * props.step))
}

onBeforeUnmount(() => {
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
})
</script>

<template>
  <div
    class="group relative z-10 shrink-0 cursor-col-resize select-none"
    :class="collapsed ? 'w-2' : 'w-1.5'"
    role="separator"
    aria-orientation="vertical"
    tabindex="0"
    :aria-label="label"
    :aria-valuenow="collapsed ? 0 : modelValue"
    :aria-valuemin="min"
    :aria-valuemax="max"
    :aria-expanded="!collapsed"
    @pointerdown="onPointerDown"
    @pointermove="onPointerMove"
    @pointerup="onPointerUp"
    @pointercancel="onPointerUp"
    @keydown="onKeydown"
    @dblclick="emit('toggle')"
  >
    <!-- 视觉上只有 1px，热区靠外层的 w-1.5 —— 1px 的线没人点得中 -->
    <span
      class="pointer-events-none absolute inset-y-0 left-1/2 w-px -translate-x-1/2 transition-colors"
      :class="dragging ? 'bg-brand' : 'bg-hairline group-hover:bg-brand-ring'"
    />
    <!-- 折叠态的展开把手：线本身太细，留一个可见的抓点 -->
    <button
      v-if="collapsed"
      type="button"
      class="absolute top-1/2 left-1/2 flex size-5 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-hairline bg-surface text-ink-3 shadow-xs transition hover:text-brand"
      :title="`展开${label}`"
      @click.stop="emit('toggle')"
    >
      <!-- 箭头指向面板将被拉回的方向：面板在左 → 朝右展开 -->
      <PhCaretDoubleRight v-if="side === 'left'" :size="11" />
      <PhCaretDoubleLeft v-else :size="11" />
    </button>
  </div>
</template>
