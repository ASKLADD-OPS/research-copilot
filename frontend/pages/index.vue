<script setup lang="ts">
/**
 * 对话页。
 *
 * 布局：会话列表 | 消息流 | 引用原文抽屉。
 * 引用角标 / 引用卡片点开右边抽屉，直接用 PDF.js 跳到那一页 ——
 * "可核查"要能一键落到原文，否则溯源就只是展示用的数字。
 */
import { PhPlus, PhTrash } from '@phosphor-icons/vue'
import type { Citation } from '~/types/api'

const chat = useChatStore()
const papers = usePapersStore()
const ui = useUiStore()

const scroller = ref<HTMLElement | null>(null)

// ---------------- 引用 → 原文抽屉 ----------------
const drawer = reactive({
  open: false,
  paperId: '',
  page: null as number | null,
  quote: '',
  title: '',
})

function openCitation(c: Citation) {
  drawer.paperId = c.paper_id
  drawer.page = c.page_start ?? null
  drawer.quote = c.quote || ''
  drawer.title = c.title || ''
  drawer.open = true
}

const pdfUrl = computed(() => (drawer.paperId ? papers.pdfUrl(drawer.paperId) : ''))

// ---------------- 滚动到底 ----------------
function scrollToBottom() {
  nextTick(() => {
    const el = scroller.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

watch(() => chat.messages.length, scrollToBottom)
watch(() => chat.answer, scrollToBottom)

/** 流式中的那条 assistant 消息：内容来自 store.answer，不是 messages 里的。 */
const streamingMessage = computed(() => {
  if (!chat.sending) return null
  return {
    id: 'streaming',
    role: 'assistant' as const,
    content: chat.answer,
    citations: [] as Citation[],
    grounding_ratio: chat.groundingRatio,
  }
})

const live = computed(() =>
  chat.sending
    ? {
        intent: chat.intent,
        plan: chat.plan,
        tools: chat.tools,
        reflections: chat.reflections,
        guardrails: chat.guardrails,
        replanNote: chat.replanNote,
        clarifyQuestion: chat.clarifyQuestion,
        latencyMs: chat.latencyMs,
      }
    : null,
)

async function onSend(query: string, paperIds: string[]) {
  await chat.send(query, paperIds)
}

onMounted(async () => {
  await Promise.all([chat.loadConversations(), papers.load()])
})
</script>

<template>
  <div class="chat-page">
    <!-- 会话列表 -->
    <aside class="sessions rc-panel">
      <div class="rc-panel-head">
        <b class="rc-panel-title rc-grow">会话</b>
        <button class="rc-btn rc-btn--ghost rc-btn--icon rc-btn--sm" type="button" title="新对话" @click="chat.newConversation()">
          <PhPlus :size="13" />
        </button>
      </div>

      <div class="sessions-body rc-scroll">
        <button
          v-for="c in chat.conversations"
          :key="c.id"
          type="button"
          class="session"
          :class="{ active: chat.activeId === c.id }"
          @click="chat.openConversation(c.id)"
        >
          <span class="session-title rc-truncate">{{ c.title || '未命名会话' }}</span>
          <span class="session-foot">
            <span class="rc-caption">{{ c.message_count }} 条</span>
            <span class="rc-spacer" />
            <span
              class="session-del"
              role="button"
              tabindex="0"
              title="删除会话"
              @click.stop="chat.removeConversation(c.id)"
              @keydown.enter.stop="chat.removeConversation(c.id)"
            >
              <PhTrash :size="11" />
            </span>
          </span>
        </button>

        <div v-if="chat.loadingConversations" class="skeletons">
          <span v-for="i in 5" :key="i" class="rc-skeleton" style="height: 34px" />
        </div>
        <div v-else-if="!chat.conversations.length" class="rc-empty">还没有历史会话</div>
      </div>
    </aside>

    <!-- 消息流 -->
    <section class="stream rc-panel">
      <div ref="scroller" class="stream-body rc-scroll">
        <div v-if="!chat.messages.length && !chat.sending" class="rc-empty welcome">
          <strong>问一个问题，或让 Agent 自己去查</strong>
          <span>
            检索链路：bge-m3 双向量召回 → RRF 融合 → 重排 → CRAG 判级。
            回答里的每个 [n] 都指向检索到的原文片段，NLI 校验不过的会标红。
          </span>
        </div>

        <ChatMessage
          v-for="m in chat.messages"
          :key="m.id"
          :message="m"
          :citations="m.citations"
          :verified-markers="chat.verifiedMarkers"
          @open-citation="openCitation"
        />

        <ChatMessage
          v-if="streamingMessage"
          :message="streamingMessage"
          :citations="chat.citations"
          :live="live"
          streaming
          :verified-markers="chat.verifiedMarkers"
          @open-citation="openCitation"
        />

        <p v-if="chat.errorMessage" class="rc-alert rc-alert--bad notice">{{ chat.errorMessage }}</p>
      </div>

      <div class="stream-foot">
        <ChatComposer
          :sending="chat.sending"
          :papers="papers.items"
          @send="onSend"
          @stop="chat.abortStream()"
        />
      </div>
    </section>

    <!-- 引用原文 -->
    <Drawer :open="drawer.open" :title="drawer.title || '引用原文'" @close="drawer.open = false">
      <div v-if="drawer.paperId" class="drawer-body">
        <ClientOnly>
          <PdfViewer :url="pdfUrl" :target-page="drawer.page" :highlight-quote="drawer.quote" />
          <template #fallback><div class="rc-empty">正在加载阅读器…</div></template>
        </ClientOnly>
      </div>
    </Drawer>
  </div>
</template>

<style scoped>
.chat-page {
  display: grid;
  grid-template-columns: 216px 1fr;
  gap: 12px;
  padding: 12px;
  height: 100vh;
}

.sessions {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.sessions-body {
  flex: 1;
  padding: 6px;
}

.session {
  display: block;
  width: 100%;
  padding: 7px 9px;
  border: none;
  border-left: 2px solid transparent;
  border-radius: var(--rc-radius-sm);
  background: transparent;
  color: inherit;
  text-align: left;
  font-size: 12.5px;
  cursor: pointer;
}
.session:hover {
  background: var(--rc-surface-2);
}
.session.active {
  background: var(--rc-primary-soft);
  border-left-color: var(--rc-primary);
}
.session-title {
  display: block;
}
.session-foot {
  display: flex;
  align-items: center;
  margin-top: 2px;
}
.session-del {
  display: inline-flex;
  padding: 2px;
  border-radius: var(--rc-radius-xs);
  color: var(--rc-ink-tertiary);
}
.session-del:hover {
  color: var(--rc-danger);
  background: var(--rc-danger-soft);
}

.skeletons {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.stream {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.stream-body {
  flex: 1;
  padding: 18px 20px 6px;
}
.stream-foot {
  padding: 6px 20px 12px;
  border-top: 1px solid var(--rc-hairline);
}

.notice {
  margin: 8px 0 0;
}

.welcome {
  min-height: 60%;
  gap: 10px;
}

.drawer-body {
  height: 100%;
}
</style>
