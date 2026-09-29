<script setup lang="ts">
/**
 * Agent 过程面板：意图 → 计划 → 工具时间线 → 反思 → 护栏。
 *
 * 这些数据在回合结束后仍保留（见 stores/chat.ts），所以历史消息回看时
 * 能看到"当时是怎么查的"，而不只是结论。默认折叠，因为它是佐证不是正文。
 */
import { PhCaretRight } from '@phosphor-icons/vue'
import type { PlanStep, Reflection } from '~/types/api'
import type { ToolTraceItem } from '~/stores/chat'

const props = withDefaults(
  defineProps<{
    intent?: { intent: string; confidence: number } | null
    plan?: PlanStep[]
    tools?: ToolTraceItem[]
    reflections?: Reflection[]
    guardrails?: { action: string; flags: string[] }[]
    replanNote?: string
    clarifyQuestion?: string
    latencyMs?: number | null
    groundingRatio?: number | null
    streaming?: boolean
  }>(),
  {
    intent: null,
    plan: () => [],
    tools: () => [],
    reflections: () => [],
    guardrails: () => [],
    replanNote: '',
    clarifyQuestion: '',
    latencyMs: null,
    groundingRatio: null,
    streaming: false,
  },
)

const open = ref(false)

/** 有内容才显示入口 —— 空面板比没有面板更糟。 */
const hasAnything = computed(
  () =>
    Boolean(props.intent) ||
    props.plan.length > 0 ||
    props.tools.length > 0 ||
    props.reflections.length > 0 ||
    props.guardrails.length > 0 ||
    Boolean(props.replanNote) ||
    Boolean(props.clarifyQuestion),
)

const summary = computed(() => {
  const bits: string[] = []
  if (props.intent) bits.push(props.intent.intent)
  if (props.tools.length) bits.push(`${props.tools.length} 次工具`)
  if (props.reflections.length) bits.push(`${props.reflections.length} 轮反思`)
  if (props.latencyMs != null) bits.push(`${(props.latencyMs / 1000).toFixed(1)}s`)
  return bits.join(' · ')
})

const DIMENSIONS = ['faithfulness', 'relevance', 'coherence', 'completeness'] as const
const DIM_LABEL: Record<string, string> = {
  faithfulness: '忠实',
  relevance: '相关',
  coherence: '连贯',
  completeness: '完整',
}

function dimScore(r: Reflection, key: string): number | null {
  const v = r.scores?.[key as keyof NonNullable<Reflection['scores']>]
  return typeof v === 'number' ? v : null
}

/** 分数用 0–1 还是 0–10 由后端决定，两种都遇过；>1 就当十分制显示。 */
function dimText(v: number | null): string {
  if (v == null) return '—'
  return v > 1 ? v.toFixed(1) : v.toFixed(2)
}

function dimPct(v: number | null): number {
  if (v == null) return 0
  return Math.max(0, Math.min(100, (v > 1 ? v / 10 : v) * 100))
}

const planStatusClass: Record<string, string> = {
  done: 'rc-pill--ok',
  succeeded: 'rc-pill--ok',
  running: 'rc-pill--primary',
  failed: 'rc-pill--bad',
  skipped: 'rc-pill--dim',
}
</script>

<template>
  <section v-if="hasAnything" class="trace">
    <button class="trace-head" type="button" :aria-expanded="open" @click="open = !open">
      <PhCaretRight class="caret" :class="{ open }" :size="12" weight="bold" />
      <span class="rc-eyebrow">推理过程</span>
      <span class="rc-mono rc-muted rc-truncate">{{ summary }}</span>
      <span v-if="streaming" class="rc-dot rc-dot--live" aria-label="进行中" />
    </button>

    <div v-if="open" class="trace-body">
      <div v-if="intent" class="block">
        <span class="block-key">意图</span>
        <div class="block-val">
          <span class="rc-pill rc-pill--primary">{{ intent.intent }}</span>
          <span class="rc-mono rc-muted">置信 {{ (intent.confidence * 100).toFixed(0) }}%</span>
        </div>
      </div>

      <div v-if="clarifyQuestion" class="block">
        <span class="block-key">澄清追问</span>
        <div class="block-val">{{ clarifyQuestion }}</div>
      </div>

      <div v-if="replanNote" class="block">
        <span class="block-key">重规划</span>
        <div class="block-val rc-muted">{{ replanNote }}</div>
      </div>

      <div v-if="plan.length" class="block">
        <span class="block-key">计划</span>
        <ol class="block-val plan">
          <li v-for="(s, i) in plan" :key="i">
            <span class="rc-mono rc-dim">{{ s.step ?? i + 1 }}</span>
            <span class="rc-grow">{{ s.goal || s.tool || '（未命名步骤）' }}</span>
            <span v-if="s.tool" class="rc-pill rc-pill--dim">{{ s.tool }}</span>
            <span v-if="s.status" class="rc-pill" :class="planStatusClass[s.status] || 'rc-pill--dim'">
              {{ s.status }}
            </span>
          </li>
        </ol>
      </div>

      <div v-if="tools.length" class="block">
        <span class="block-key">工具</span>
        <ol class="block-val plan">
          <li v-for="(t, i) in tools" :key="i">
            <span class="rc-dot" :class="t.status === 'running' ? 'rc-dot--live' : 'rc-dot--ok'" />
            <span class="rc-mono">{{ t.name }}</span>
            <span v-if="t.detail" class="rc-muted rc-truncate rc-grow">{{ t.detail }}</span>
          </li>
        </ol>
      </div>

      <div v-if="reflections.length" class="block">
        <span class="block-key">反思</span>
        <div class="block-val reflect">
          <div v-for="(r, i) in reflections" :key="i" class="reflect-round">
            <div class="rc-row">
              <span class="rc-mono rc-dim">第 {{ r.round ?? i + 1 }} 轮</span>
              <span v-if="r.verdict" class="rc-pill" :class="r.verdict === 'pass' ? 'rc-pill--ok' : 'rc-pill--warn'">
                {{ r.verdict }}
              </span>
              <span class="rc-spacer" />
              <span v-if="r.overall != null" class="rc-mono">{{ dimText(r.overall) }}</span>
            </div>
            <div class="dims">
              <div v-for="d in DIMENSIONS" :key="d" class="dim">
                <span class="rc-caption">{{ DIM_LABEL[d] }}</span>
                <span class="meter" :style="{ width: `${dimPct(dimScore(r, d))}%` }" />
                <span class="rc-mono rc-dim">{{ dimText(dimScore(r, d)) }}</span>
              </div>
            </div>
            <p v-if="r.critique" class="critique">{{ r.critique }}</p>
          </div>
        </div>
      </div>

      <div v-if="guardrails.length" class="block">
        <span class="block-key">护栏</span>
        <div class="block-val plan">
          <div v-for="(g, i) in guardrails" :key="i" class="rc-row">
            <span class="rc-pill" :class="g.action === 'block' ? 'rc-pill--bad' : 'rc-pill--warn'">{{ g.action }}</span>
            <span class="rc-muted rc-truncate">{{ g.flags.join(' · ') }}</span>
          </div>
        </div>
      </div>

      <div v-if="groundingRatio != null" class="block">
        <span class="block-key">有据率</span>
        <div class="block-val rc-row">
          <span class="rc-mono">{{ (groundingRatio * 100).toFixed(1) }}%</span>
          <span class="rc-muted">（阈值 80%，低于则标注低置信）</span>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.trace {
  margin-top: 10px;
  border: 1px solid var(--rc-hairline);
  border-radius: var(--rc-radius-md);
  background: var(--rc-surface-1);
  overflow: hidden;
}

.trace-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 10px;
  background: transparent;
  border: none;
  cursor: pointer;
  text-align: left;
}
.trace-head:hover {
  background: var(--rc-surface-2);
}
.caret {
  color: var(--rc-ink-tertiary);
  transition: transform 0.14s ease;
  flex: 0 0 auto;
}
.caret.open {
  transform: rotate(90deg);
}

.trace-body {
  padding: 4px 10px 10px;
  border-top: 1px solid var(--rc-hairline);
}

.block {
  display: grid;
  grid-template-columns: 62px 1fr;
  gap: 10px;
  padding: 7px 0;
}
.block + .block {
  border-top: 1px solid var(--rc-hairline);
}
.block-key {
  font-size: 11.5px;
  color: var(--rc-ink-tertiary);
  padding-top: 2px;
}
.block-val {
  min-width: 0;
  font-size: 12.5px;
  color: var(--rc-ink-muted);
}

.plan {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.plan li {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}

.reflect-round + .reflect-round {
  margin-top: 9px;
  padding-top: 9px;
  border-top: 1px dashed var(--rc-hairline);
}
.dims {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 14px;
  margin-top: 6px;
}
.dim {
  display: grid;
  grid-template-columns: 34px 1fr 34px;
  align-items: center;
  gap: 6px;
}
/* 只有填充条、没有底色轨道 —— 有轨道的进度条是仪表盘噪音 */
.meter {
  height: 3px;
  border-radius: var(--rc-radius-pill);
  background: var(--rc-primary);
}
.critique {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--rc-ink-tertiary);
  line-height: 1.6;
}
</style>
