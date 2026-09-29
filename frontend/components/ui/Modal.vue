<script setup lang="ts">
/**
 * 模态框：Teleport 到 body + 遮罩 + Esc 关闭 + 锁滚动。
 *
 * 用原生 `<dialog>` 本来更省事，但它的 ::backdrop 无法做毛玻璃、
 * 且 `showModal()` 的焦点陷阱在 SSR 下有 hydration 时序问题。这里自己实现反而更短。
 */
import { PhX } from '@phosphor-icons/vue'

const props = withDefaults(
  defineProps<{ modelValue: boolean; title: string; width?: string; closeOnBackdrop?: boolean }>(),
  { width: '460px', closeOnBackdrop: true },
)

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

function close() {
  emit('update:modelValue', false)
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') close()
}

// 挂载期间锁 body 滚动：不锁的话滚轮会穿透到下面的文献列表，模态框里反而不能滚
watch(
  () => props.modelValue,
  (open) => {
    if (!import.meta.client) return
    document.body.style.overflow = open ? 'hidden' : ''
    if (open) window.addEventListener('keydown', onKeydown)
    else window.removeEventListener('keydown', onKeydown)
  },
)

onBeforeUnmount(() => {
  if (!import.meta.client) return
  document.body.style.overflow = ''
  window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-120"
      enter-from-class="opacity-0"
      leave-active-class="transition duration-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="modelValue"
        class="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-ink/25 p-4 pt-[12vh] backdrop-blur-[2px]"
        role="dialog"
        aria-modal="true"
        @click.self="closeOnBackdrop && close()"
      >
        <div
          class="w-full max-w-full overflow-hidden rounded-xl border border-hairline bg-surface shadow-lg"
          :style="{ width }"
        >
          <header class="flex h-10 items-center gap-2 border-b border-hairline px-3">
            <h3 class="truncate text-[13px] font-semibold text-ink">{{ title }}</h3>
            <span class="flex-1" />
            <button
              type="button"
              class="grid size-6 place-items-center rounded-sm text-ink-3 transition-colors hover:bg-hover hover:text-ink"
              aria-label="关闭"
              @click="close"
            >
              <PhX :size="13" />
            </button>
          </header>

          <div class="max-h-[62vh] overflow-y-auto scroll-slim p-3">
            <slot />
          </div>

          <footer v-if="$slots.footer" class="flex items-center justify-end gap-2 border-t border-hairline px-3 py-2.5">
            <slot name="footer" />
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
