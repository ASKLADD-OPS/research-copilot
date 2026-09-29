<script setup lang="ts">
/**
 * 一问一答。
 *
 * 用户侧渲染纯文本（`whitespace-pre-wrap`），助手侧走 Markdown —— 用户输入里
 * 不该被解析成 Markdown，他打了个 `#` 就该看到 `#`。
 */
import { PhUser } from '@phosphor-icons/vue'
import type { ChatMessage, Citation } from '~/types/api'

const props = withDefaults(
  defineProps<{ message: ChatMessage; streaming?: boolean; isLast?: boolean }>(),
  { streaming: false, isLast: false },
)

const chat = useChatStore()
const selection = useSelectionStore()
const ui = useUiStore()

const isUser = computed(() => props.message.role === 'user')

/**
 * 已校验的角标集合。
 * 正在流式输出的那一轮用 store 里的实时集合；历史消息只能用自己带的 citations 反推。
 */
const verified = computed<Set<number>>(() => {
  if (props.streaming || (props.isLast && chat.sending)) return chat.verifiedMarkers
  const set = new Set<number>()
  for (const c of props.message.citations ?? []) {
    if (c.verified && c.marker != null) set.add(c.marker)
  }
  return set
})

const citations = computed<Citation[]>(() => {
  if (props.streaming) return chat.citations
  return props.message.citations ?? []
})

/** 角标点击 → 跳原文。用事件委托，避免给每个角标挂监听。 */
function onContentClick(e: MouseEvent) {
  const target = (e.target as HTMLElement).closest('.cite-mark')
  if (!target) return
  const marker = Number((target as HTMLElement).dataset.marker)
  if (!Number.isFinite(marker)) return
  const cite = (
    props.streaming ? chat.citations : (props.message.citations ?? [])
  ).find((c) => c.marker === marker)
  if (!cite) return
  ui.setMainView('reader')
  void selection.openAt(cite.paper_id, cite.page_start ?? 1, cite.quote)
}
</script>

<template>
  <article class="group px-2.5 py-2.5">
    <div v-if="isUser" class="flex items-start gap-2">
      <span class="mt-0.5 grid size-5 shrink-0 place-items-center rounded-md bg-sunken text-ink-3">
        <PhUser :size="11" />
      </span>
      <p class="min-w-0 flex-1 text-[12.5px] leading-relaxed whitespace-pre-wrap text-ink">
        {{ message.content }}
      </p>
    </div>

    <div v-else class="min-w-0">
      <div @click="onContentClick">
        <MarkdownView :source="message.content" :verified="verified" :streaming="streaming" />
      </div>

      <div v-if="!streaming && !message.content" class="text-2xs text-ink-4">（本次回答为空）</div>

      <div v-if="citations.length" class="mt-2 border-t border-hairline pt-2">
        <p class="mb-1 flex items-center gap-1.5 text-2xs font-medium text-ink-3">
          引用 {{ citations.length }}
          <span v-if="message.grounding_ratio != null" :class="pillCls(groundingTone(message.grounding_ratio))">
            有据率 {{ (message.grounding_ratio * 100).toFixed(0) }}%
          </span>
          <span v-if="message.intent" :class="pillCls('brand')">
            {{ INTENT_LABEL[message.intent as keyof typeof INTENT_LABEL] ?? message.intent }}
          </span>
        </p>
        <CitationList :citations="citations" />
      </div>

      <!-- 轨迹只属于最后一条：历史回合的 plan/tools 没有留档，不假装有 -->
      <div v-if="isLast" class="mt-2">
        <AgentTrace />
      </div>
    </div>
  </article>
</template>
