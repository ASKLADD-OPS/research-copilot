<script setup lang="ts">
import { PhCaretDown } from '@phosphor-icons/vue'

/**
 * FAQ 折叠面板。
 *
 * 动画手法：用 `grid-template-rows: 0fr → 1fr` 过渡，而不是给内容测高度写 max-height。
 * 后者要么写死一个魔数（长了会被截、短了会延迟），要么每次展开去量 scrollHeight
 * 并触发强制同步布局。grid 这一招能直接过渡到"内容自然高度"，无需任何 JS 测量。
 *
 * 无障碍：按钮带 aria-expanded / aria-controls，面板带 role=region + aria-labelledby。
 * 面板在收起时用 `invisible` 摘出可访问性树与 Tab 序列 —— 只设 grid 0fr 的话，
 * 屏幕阅读器仍然会读到"看不见"的内容，Tab 也会跳进看不见的链接。
 */
interface FaqItem {
  q: string
  a: string
}

const props = defineProps<{ items: readonly FaqItem[] }>()

/** 用 Set 而不是单个 index：多条问答各自独立展开，比手风琴更少打断用户。 */
const open = ref<Set<number>>(new Set())

function toggle(i: number) {
  const next = new Set(open.value)
  if (next.has(i)) next.delete(i)
  else next.add(i)
  open.value = next
}
</script>

<template>
  <div class="divide-y divide-hairline overflow-hidden rounded-xl border border-hairline bg-surface">
    <div v-for="(item, i) in props.items" :key="item.q">
      <h3>
        <button
          :id="`faq-btn-${i}`"
          type="button"
          class="flex w-full items-center gap-4 px-5 py-4 text-left transition-colors duration-[var(--dur-fast)] hover:bg-hover"
          :aria-expanded="open.has(i)"
          :aria-controls="`faq-panel-${i}`"
          @click="toggle(i)"
        >
          <span class="flex-1 text-[14.5px] font-medium text-ink">{{ item.q }}</span>
          <PhCaretDown
            :size="15"
            class="shrink-0 text-ink-3 transition-transform duration-[var(--dur-base)]"
            :class="open.has(i) ? 'rotate-180' : ''"
          />
        </button>
      </h3>

      <div
        :id="`faq-panel-${i}`"
        role="region"
        :aria-labelledby="`faq-btn-${i}`"
        class="grid transition-[grid-template-rows] duration-[var(--dur-base)] ease-out"
        :style="{ gridTemplateRows: open.has(i) ? '1fr' : '0fr' }"
      >
        <div class="overflow-hidden">
          <p
            class="px-5 pb-5 text-[13.5px] leading-relaxed text-ink-2 transition-opacity duration-[var(--dur-base)]"
            :class="open.has(i) ? 'opacity-100' : 'invisible opacity-0'"
          >
            {{ item.a }}
          </p>
        </div>
      </div>
    </div>
  </div>
</template>
