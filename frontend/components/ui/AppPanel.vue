<script setup lang="ts">
/**
 * 面板外壳：标题栏 + 内容槽 + 折叠态。
 *
 * 只抽外壳、不抽按钮：按钮在这个界面里出现上百次，做成组件会带来一堆
 * 只用两三次的 props（size / tone / loading / icon …），而工具类写出来就是
 * 一行。真正值得抽成组件的是"有生命周期或状态机"的东西 —— 面板就是其中之一
 * （折叠态要换一套完全不同的渲染）。
 */
import type { Component } from 'vue'
import { PhCaretLeft, PhCaretRight } from '@phosphor-icons/vue'

const props = withDefaults(
  defineProps<{
    title: string
    icon?: Component
    collapsed?: boolean
    collapsible?: boolean
    /** 面板在分割线的哪一侧，决定折叠箭头朝里还是朝外 */
    side?: 'left' | 'right'
    /** 标题栏右侧的副信息，如"12 篇 / 3 进行中" */
    meta?: string
  }>(),
  { collapsed: false, collapsible: true, side: 'left', meta: '' },
)

const emit = defineEmits<{ toggle: [] }>()
</script>

<template>
  <section class="flex h-full w-full flex-col overflow-hidden bg-surface">
    <!-- 折叠态：竖排标题的窄条，点击展开。整条都是按钮，不留死区。 -->
    <button
      v-if="collapsed"
      type="button"
      class="flex h-full w-full flex-col items-center gap-3 py-3 text-ink-3 transition-colors hover:bg-hover hover:text-ink"
      :title="`展开「${title}」`"
      @click="emit('toggle')"
    >
      <component :is="icon" v-if="icon" :size="15" />
      <PhCaretRight v-if="side === 'left'" :size="11" />
      <PhCaretLeft v-else :size="11" />
      <span class="mt-0.5 text-2xs font-medium tracking-wide [writing-mode:vertical-rl]">{{ title }}</span>
    </button>

    <template v-else>
      <header
        class="flex h-9 shrink-0 items-center gap-1.5 border-b border-hairline px-2.5"
      >
        <!-- title 插槽用于"标题本身就是控件"的场景（如右栏的标签切换）。
             title 属性仍必须给 —— 折叠态的竖排文字要用它。 -->
        <slot name="title">
          <component :is="icon" v-if="icon" :size="14" class="shrink-0 text-ink-3" />
          <h2 class="truncate text-[12.5px] font-semibold text-ink">{{ title }}</h2>
        </slot>
        <span v-if="meta" class="shrink-0 text-2xs text-ink-3">{{ meta }}</span>
        <span class="min-w-0 flex-1" />
        <slot name="actions" />
        <button
          v-if="collapsible"
          type="button"
          class="grid size-6 shrink-0 place-items-center rounded-sm text-ink-4 transition-colors hover:bg-hover hover:text-ink"
          :title="`收起「${title}」`"
          @click="emit('toggle')"
        >
          <PhCaretLeft v-if="side === 'left'" :size="12" />
          <PhCaretRight v-else :size="12" />
        </button>
      </header>

      <div class="min-h-0 flex-1 overflow-hidden">
        <slot />
      </div>

      <footer v-if="$slots.footer" class="shrink-0 border-t border-hairline">
        <slot name="footer" />
      </footer>
    </template>
  </section>
</template>
