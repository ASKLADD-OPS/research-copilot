<script setup lang="ts">
/**
 * 对话面板（纯内容，外壳由 WorkPanel 提供）。
 *
 * 消息列表在流式期间会拿到一个"合成的"最后一条：它的正文直接读 store 的
 * `chat.answer`，而不是往 messages 数组里塞临时对象。这样 token 追加只改一个
 * 字符串，不会触发整个列表的 diff。
 */
import { PhChatsCircle, PhPlus, PhTrash, PhChatCircleDots } from '@phosphor-icons/vue'
import type { ChatMessage } from '~/types/api'

const chat = useChatStore()
const selection = useSelectionStore()

const listEl = ref<HTMLElement | null>(null)

/** 屏幕上真正要渲染的列表 = 已固化的消息 + （流式中）一个合成回合。 */
const visible = computed<ChatMessage[]>(() => {
  if (!chat.sending && !chat.answer) return chat.messages
  return [
    ...chat.messages,
    { id: 'live', role: 'assistant', content: chat.answer, citations: [] } as ChatMessage,
  ]
})

/** 新内容到达时贴底，但只在用户本来就在底部时才自动滚 —— 否则他一往上翻就被拽回来。 */
function pinnedToBottom() {
  const el = listEl.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight < 80
}

watch(
  () => [visible.value.length, chat.answer],
  async () => {
    const stick = pinnedToBottom()
    await nextTick()
    const el = listEl.value
    if (el && stick) el.scrollTop = el.scrollHeight
  },
)

function onPickConversation(e: Event) {
  const id = (e.target as HTMLSelectElement).value
  if (id) void chat.openConversation(id)
}

onMounted(() => void chat.loadConversations())
</script>

<template>
  <div class="flex h-full flex-col">
    <!-- 会话切换 -->
    <div class="flex shrink-0 items-center gap-1.5 border-b border-hairline px-2.5 py-1.5">
      <select
        :class="SELECT_CLS"
        :value="chat.activeId ?? ''"
        aria-label="历史会话"
        @change="onPickConversation"
      >
        <option value="">
          {{ chat.conversations.length ? '新会话（未保存）' : '还没有历史会话' }}
        </option>
        <option v-for="c in chat.conversations" :key="c.id" :value="c.id">
          {{ c.title || '未命名' }}（{{ c.message_count }}）
        </option>
      </select>
      <button type="button" :class="iconBtnCls('sm')" title="新建会话" @click="chat.newConversation()">
        <PhPlus :size="12" />
      </button>
      <button
        v-if="chat.activeId"
        type="button"
        :class="iconBtnCls('sm')"
        title="删除当前会话"
        @click="chat.removeConversation(chat.activeId)"
      >
        <PhTrash :size="12" />
      </button>
    </div>

    <!-- 消息 -->
    <div ref="listEl" class="min-h-0 flex-1 overflow-y-auto scroll-slim">
      <div v-if="chat.loadingMessages" class="space-y-2 p-2.5">
        <span v-for="i in 4" :key="i" class="block h-10 animate-pulse rounded-md bg-sunken" />
      </div>

      <div v-else-if="!visible.length" :class="EMPTY_CLS" class="pt-10">
        <PhChatsCircle :size="20" class="mb-1.5 text-ink-4" />
        <strong class="text-[12.5px] text-ink-2">开始一段研究对话</strong>
        <span class="max-w-64 text-2xs leading-relaxed text-ink-4">
          检索范围决定它能读哪些文献；在阅读器里划一段文字再点「追问这段」，问题会带着原文一起发出去。
        </span>
        <span v-if="selection.active" class="mt-2 max-w-64 text-2xs text-ink-3">
          当前打开：{{ selection.active.title }}
        </span>
      </div>

      <template v-else>
        <ChatTurn
          v-for="(m, i) in visible"
          :key="m.id"
          :message="m"
          :streaming="chat.sending && i === visible.length - 1"
          :is-last="i === visible.length - 1"
        />
      </template>
    </div>

    <p v-if="chat.errorMessage" class="shrink-0 bg-bad-soft px-2.5 py-1.5 text-2xs text-bad">
      <PhChatCircleDots :size="11" class="mr-1 inline align-[-1px]" />
      {{ chat.errorMessage }}
    </p>

    <ChatComposer />
  </div>
</template>
