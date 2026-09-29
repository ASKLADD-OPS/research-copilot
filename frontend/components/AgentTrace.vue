<script setup lang="ts">
/**
 * Agent 运行轨迹。读 chat store 而不是收 props：它是这一轮"实时状态"的视图，
 * 而实时状态只存在于 store 里（回合结束后只有答案与引用会被固化到消息上，轨迹本身不留）。
 *
 * 默认折叠。理由是它每轮都出现但只在出问题时才需要看 —— 常驻展开会把对话
 * 挤成一行字，而"为什么这么答"的需求是偶发的、强烈的（那时才展开）。
 */
import {
  PhCaretDown,
  PhCaretRight,
  PhGauge,
  PhListChecks,
  PhShieldWarning,
  PhTarget,
  PhWrench,
} from '@phosphor-icons/vue'

const chat = useChatStore()
const open = ref(false)

const DIM_LABEL = {
  faithfulness: '忠实',
  relevance: '相关',
  coherence: '连贯',
  completeness: '完整',
} as const

type Dim = keyof typeof DIM_LABEL
const DIMS = Object.keys(DIM_LABEL) as Dim[]

const lastReflection = computed(() => chat.reflections.at(-1) ?? null)

const summary = computed(() => {
  const bits: string[] = []
  if (chat.intent) {
    bits.push(`${INTENT_LABEL[chat.intent.intent] ?? chat.intent.intent} ${(chat.intent.confidence * 100).toFixed(0)}%`)
  }
  if (chat.plan.length) bits.push(`${chat.plan.length} 步`)
  if (chat.tools.length) bits.push(`${chat.tools.length} 次工具`)
  if (lastReflection.value?.overall != null) bits.push(`反思 ${lastReflection.value.overall.toFixed(2)}`)
  return bits.join(' · ')
})

function barTone(v: number) {
  if (v >= 0.8) return 'bg-ok'
  if (v >= 0.6) return 'bg-warn'
  return 'bg-bad'
}
</script>

<template>
  <div v-if="chat.hasTrace" class="rounded-lg border border-hairline bg-surface">
    <button
      type="button"
      class="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left"
      @click="open = !open"
    >
      <component :is="open ? PhCaretDown : PhCaretRight" :size="11" class="shrink-0 text-ink-4" />
      <span class="shrink-0 text-2xs font-medium text-ink-3">Agent 轨迹</span>
      <span class="min-w-0 flex-1 truncate text-2xs text-ink-4">{{ summary }}</span>
      <span
        v-if="chat.groundingRatio != null"
        :class="pillCls(groundingTone(chat.groundingRatio))"
        title="有据率：回答中被检索上下文支撑的比例，≥0.8 才算通过"
      >
        有据率 {{ (chat.groundingRatio * 100).toFixed(0) }}%
      </span>
    </button>

    <div v-if="open" class="space-y-2.5 border-t border-hairline px-2.5 py-2">
      <!-- 歧义追问：意图置信度低于阈值时 Agent 会先问回来 -->
      <div v-if="chat.clarifyQuestion" class="rounded-md bg-warn-soft px-2 py-1.5 text-2xs text-warn">
        <b>需要澄清：</b>{{ chat.clarifyQuestion }}
      </div>

      <div v-if="chat.plan.length">
        <div class="mb-1 flex items-center gap-1.5 text-2xs font-medium text-ink-3">
          <PhListChecks :size="11" />
          计划
        </div>
        <ol class="space-y-0.5">
          <li v-for="(s, i) in chat.plan" :key="i" class="flex items-start gap-1.5 text-2xs leading-relaxed">
            <span class="mt-px w-4 shrink-0 text-right tabular-nums text-ink-4">{{ i + 1 }}</span>
            <span class="min-w-0 flex-1 text-ink-2">{{ s.goal || '（无描述）' }}</span>
            <span v-if="s.tool" :class="pillCls('default')">{{ s.tool }}</span>
            <span v-if="s.status" class="shrink-0 text-ink-4">{{ s.status }}</span>
          </li>
        </ol>
      </div>

      <div v-if="chat.tools.length">
        <div class="mb-1 flex items-center gap-1.5 text-2xs font-medium text-ink-3">
          <PhWrench :size="11" />
          工具调用
        </div>
        <ul class="space-y-0.5">
          <li v-for="(t, i) in chat.tools" :key="i" class="flex items-center gap-1.5 text-2xs">
            <span
              class="size-1.5 shrink-0 rounded-full"
              :class="t.status === 'running' ? 'bg-brand' : 'bg-ok'"
            />
            <span class="font-mono text-ink-2">{{ t.name }}</span>
            <span v-if="t.detail" class="min-w-0 flex-1 truncate text-ink-4">{{ t.detail }}</span>
          </li>
        </ul>
      </div>

      <!-- 反思：4 维打分。低于阈值会触发 refine，这里要能看出是哪一维拖后腿 -->
      <div v-if="lastReflection">
        <div class="mb-1 flex items-center gap-1.5 text-2xs font-medium text-ink-3">
          <PhGauge :size="11" />
          反思（第 {{ (lastReflection.round ?? 0) + 1 }} 轮）
          <span v-if="lastReflection.verdict" :class="pillCls('default')">{{ lastReflection.verdict }}</span>
        </div>
        <div class="space-y-1">
          <div v-for="d in DIMS" :key="d" class="flex items-center gap-2">
            <span class="w-8 shrink-0 text-2xs text-ink-4">{{ DIM_LABEL[d] }}</span>
            <span class="h-1 min-w-0 flex-1 overflow-hidden rounded-full bg-sunken">
              <span
                class="block h-full rounded-full"
                :class="barTone(lastReflection.scores?.[d] ?? 0)"
                :style="{ width: `${(lastReflection.scores?.[d] ?? 0) * 100}%` }"
              />
            </span>
            <span class="w-7 shrink-0 text-right text-2xs tabular-nums text-ink-3">
              {{ ((lastReflection.scores?.[d] ?? 0) * 100).toFixed(0) }}
            </span>
          </div>
        </div>
        <p v-if="lastReflection.critique" class="mt-1 text-2xs leading-relaxed text-ink-4">
          {{ lastReflection.critique }}
        </p>
      </div>

      <div v-if="chat.replanNote" class="flex items-start gap-1.5 text-2xs text-ink-3">
        <PhTarget :size="11" class="mt-px shrink-0" />
        <span>重规划：{{ chat.replanNote }}</span>
      </div>

      <div v-if="chat.guardrails.length" class="space-y-1">
        <div
          v-for="(g, i) in chat.guardrails"
          :key="i"
          class="rounded-md bg-warn-soft px-2 py-1.5 text-2xs text-warn"
        >
          <span class="inline-flex items-center gap-1.5">
            <PhShieldWarning :size="11" />
            <b>{{ g.action }}</b>
            <span v-if="g.flags.length">{{ g.flags.join(' / ') }}</span>
          </span>
        </div>
      </div>

      <p class="border-t border-hairline pt-1.5 text-2xs text-ink-4">
        <span v-if="chat.latencyMs != null">{{ (chat.latencyMs / 1000).toFixed(1) }}s</span>
        <span v-if="Object.keys(chat.usage).length"> · {{ chat.usage.total_tokens ?? 0 }} tok</span>
      </p>
    </div>
  </div>
</template>
