<script setup lang="ts">
/**
 * 一条消息。用户消息只渲染正文；助手消息带引用卡片与推理过程面板。
 *
 * 头像用方角 + 品牌色，不用圆形照片：这是内部工具，没有人格化需求，
 * 而"人像占位图"是 AI 生成界面的高发套话。
 */
import type { Citation, ChatMessage as ChatMessageType } from '~/types/api'
import type { ToolTraceItem } from '~/stores/chat'
import type { PlanStep, Reflection } from '~/types/api'

const props = withDefaults(
  defineProps<{
    message: ChatMessageType & { live?: unknown }
    citations?: Citation[]
    verifiedMarkers?: Set<number>
    streaming?: boolean
    live?: {
      intent?: { intent: string; confidence: number } | null
      plan?: PlanStep[]
      tools?: ToolTraceItem[]
      reflections?: Reflection[]
      guardrails?: { action: string; flags: string[] }[]
      replanNote?: string
      clarifyQuestion?: string
      latencyMs?: number | null
    } | null
  }>(),
  {
    citations: () => [],
    verifiedMarkers: undefined,
    streaming: false,
    live: null,
  },
)

const emit = defineEmits<{ 'open-citation': [citation: Citation] }>()

const isUser = computed(() => props.message.role === 'user')
const isSystem = computed(() => props.message.role === 'system')
</script>

<template>
  <article class="msg" :class="{ user: isUser, system: isSystem }">
    <span class="avatar" aria-hidden="true">{{ isUser ? '你' : 'RC' }}</span>

    <div class="body">
      <MarkdownView
        v-if="!isUser"
        :source="message.content || (streaming ? '' : '（空回答）')"
        :verified="verifiedMarkers"
        :streaming="streaming"
      />
      <p v-else class="user-text">{{ message.content }}</p>

      <div v-if="message.grounding_ratio != null" class="rc-row foot">
        <span class="rc-pill" :class="message.grounding_ratio >= 0.8 ? 'rc-pill--ok' : 'rc-pill--warn'">
          有据率 {{ (message.grounding_ratio * 100).toFixed(0) }}%
        </span>
        <span v-if="message.intent" class="rc-pill rc-pill--dim">{{ message.intent }}</span>
      </div>

      <AgentTrace
        v-if="!isUser && live"
        :intent="live.intent"
        :plan="live.plan"
        :tools="live.tools"
        :reflections="live.reflections"
        :guardrails="live.guardrails"
        :replan-note="live.replanNote"
        :clarify-question="live.clarifyQuestion"
        :latency-ms="live.latencyMs"
        :streaming="streaming"
      />

      <div v-if="!isUser && citations.length" class="cites">
        <CitationCard
          v-for="(c, i) in citations"
          :key="`${c.marker}-${c.chunk_id}`"
          :citation="c"
          :index="i + 1"
          @open="emit('open-citation', $event)"
        />
      </div>
    </div>
  </article>
</template>

<style scoped>
.msg {
  display: flex;
  gap: 10px;
  margin-bottom: var(--rc-space-lg);
}
.msg.user {
  flex-direction: row-reverse;
}
.msg.system {
  opacity: 0.85;
}

.avatar {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  flex: 0 0 24px;
  border-radius: var(--rc-radius-sm);
  background: var(--rc-surface-2);
  color: var(--rc-ink-subtle);
  font-family: var(--rc-font-display);
  font-size: 10.5px;
  font-weight: 600;
}
.msg:not(.user) .avatar {
  background: var(--rc-primary-soft);
  color: var(--rc-primary-hover);
}

.body {
  min-width: 0;
  max-width: min(860px, 82%);
}
.msg.user .body {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
}

.user-text {
  margin: 0;
  padding: 8px 12px;
  background: var(--rc-surface-2);
  border: 1px solid var(--rc-hairline);
  border-radius: var(--rc-radius-lg);
  font-size: 13.5px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.foot {
  margin-top: 8px;
}

.cites {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 8px;
  margin-top: 10px;
}

@media (max-width: 768px) {
  .body {
    max-width: 100%;
  }
  .cites {
    grid-template-columns: 1fr;
  }
}
</style>
