<script setup lang="ts">
/**
 * 对话框。Teleport 到 body + 手写遮罩。
 *
 * 没用原生 `<dialog>`：它的 `::backdrop` 跟自定义过渡配合很别扭，Esc 也要额外接管。
 * 这里自己收：Esc 关、点遮罩关、打开时锁 body 滚动、焦点移进面板。
 *
 * ponytail: 没做焦点陷阱 —— 这些弹窗里最多两三个表单控件，锁滚动 + 自动聚焦
 * 已经够用；等出现"Tab 能跑到背景页面"的真实投诉再引 focus-trap。
 */
const props = withDefaults(defineProps<{ open: boolean; title?: string; width?: string }>(), {
  title: '',
  width: '',
})

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

    <Transition name="rc-pop">
      <div
        v-if="open"
        ref="panel"
        class="rc-modal"
        role="dialog"
        aria-modal="true"
        :aria-label="title || '对话框'"
        tabindex="-1"
        :style="width ? { width } : undefined"
      >
        <header class="rc-overlay-head">
          <h2 class="rc-panel-title rc-grow">{{ title }}</h2>
          <slot name="head-extra" />
          <button class="rc-btn rc-btn--ghost rc-btn--icon rc-btn--sm" type="button" aria-label="关闭" @click="emit('close')">
            <slot name="close-icon">✕</slot>
          </button>
        </header>

        <div class="rc-overlay-body rc-scroll" style="padding: 14px">
          <slot />
        </div>

        <footer v-if="$slots.footer" class="rc-overlay-foot">
          <slot name="footer" />
        </footer>
      </div>
    </Transition>
  </Teleport>
</template>
