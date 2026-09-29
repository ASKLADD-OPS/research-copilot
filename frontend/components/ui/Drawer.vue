<script setup lang="ts">
/**
 * 右侧抽屉。与 Modal 同一套行为（Esc / 遮罩 / 锁滚动），只是定位与过渡不同 ——
 * 合并成一个组件要靠 4 个布尔 props 描述"像哪个"，反而更难读。
 */
const props = withDefaults(defineProps<{ open: boolean; title?: string }>(), { title: '' })

const emit = defineEmits<{ close: [] }>()

const panel = ref<HTMLElement | null>(null)

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close')
}

watch(
  () => props.open,
  (open) => {
    if (!import.meta.client) return
    document.body.style.overflow = open ? 'hidden' : ''
    if (open) {
      window.addEventListener('keydown', onKeydown)
      void nextTick(() => panel.value?.focus())
    } else {
      window.removeEventListener('keydown', onKeydown)
    }
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
    <Transition name="rc-fade">
      <div v-if="open" class="rc-scrim" @click="emit('close')" />
    </Transition>

    <Transition name="rc-slide">
      <div
        v-if="open"
        ref="panel"
        class="rc-drawer"
        role="dialog"
        aria-modal="true"
        :aria-label="title || '侧栏'"
        tabindex="-1"
      >
        <header class="rc-overlay-head">
          <h2 class="rc-panel-title rc-grow rc-truncate">{{ title }}</h2>
          <slot name="head-extra" />
          <button class="rc-btn rc-btn--ghost rc-btn--icon rc-btn--sm" type="button" aria-label="关闭" @click="emit('close')">
            ✕
          </button>
        </header>

        <div class="rc-overlay-body">
          <slot />
        </div>

        <footer v-if="$slots.footer" class="rc-overlay-foot">
          <slot name="footer" />
        </footer>
      </div>
    </Transition>
  </Teleport>
</template>
