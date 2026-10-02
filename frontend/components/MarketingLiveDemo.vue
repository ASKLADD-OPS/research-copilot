<script setup lang="ts">
/**
 * 首页实时演示区：把 LLM 的流式回答用 SSE 摊开给你看。
 *
 * 这不是截图，是**真的在打后端** `POST /api/v1/chat/stream`。三块内容并列：
 *   1. 渲染后的答案（token 事件逐字追加）
 *   2. Agent 调用链（intent / plan / tool / reflection / guardrail 事件）
 *   3. 原始 SSE 帧（`event:` + `data:` 原文，用 onRawFrame 旁路拿到）
 *
 * 为什么展示原始帧：这个产品的卖点之一就是"多智能体、全链路可追溯"。
 * 把协议本身摆出来，比任何文案都有说服力 —— 用户能亲眼看到答案不是一次性返回的，
 * 而是"意图识别 → 规划 → 检索 → 反思 → 合成"一段段推出来的。
 *
 * 刻意不复用 `useChatStore`：那是工作台的会话状态（要落库、要进历史列表）。
 * 首页演示是一次性的、无副作用的，直接挂在 `useChatStream` 上更干净。
 */
import {
  PhCircleNotch,
  PhPaperPlaneRight,
  PhStop,
  PhCaretDown,
  PhWarningCircle,
  PhCrosshair,
} from '@phosphor-icons/vue'
import { INTENT_LABEL } from '~/stores/chat'
import type { StreamEvent, Citation, PlanStep, IntentKind } from '~/types/api'

interface TraceItem {
  kind: 'intent' | 'plan' | 'tool' | 'citation' | 'reflection' | 'replan' | 'guardrail' | 'clarify'
  label: string
  detail?: string
}

// 预设必须按**意图置信度的余量**挑，不是按"跑通过一次"挑。
// 后端 classify 低于 0.6 会转澄清追问（不出 token），而这些问句的置信度是浮动的 ——
// 同一条问句每轮都可能落在阈值两侧。实测（本机降级环境，各 3–4 轮）：
//   translation「把这句话翻译成…」0.95–0.96  → 4/4 出正文，且跑完 Plan → 反思 → 重规划
//   translation「把下面这句话翻成中文…」0.96  → 3/3
//   chitchat「你能做什么？」0.95–0.97         → 4/4，最快
// 已被换掉的坑：visualization「画一张检索流程的图」实测 0.58–0.63，**会在 0.6 下面抖**（3 轮里 1 轮转澄清），
//   writing_assist「帮我写一段论文摘要的开头」0.84–0.88，4 轮里 2 轮零 token（模型层偶发空返回）。
// 换预设前请照这个方法量几轮余量（直接打 POST /api/v1/chat/stream，读 intent 帧的
// confidence，数 token 帧）：**别只看一次结果**，0.6 附近的问句同一句话每轮都可能翻车。
const PRESETS = [
  '你能做什么？',
  '把这句话翻译成英文：混合检索优于单路检索，但需要重排兜底。',
  '把下面这句话翻成中文：Retrieval-augmented generation grounds answers in cited evidence.',
] as const

const question = ref('')
const answer = ref('')
const running = ref(false)
const trace = ref<TraceItem[]>([])
const frames = ref<string[]>([])
const errMsg = ref('')
const intentInfo = ref<{ intent: IntentKind; confidence: number } | null>(null)
const meta = ref<{ grounding?: number; latency?: number; tokens?: number; citations?: number }>({})
const showFrames = ref(false)

const answerBox = ref<HTMLElement | null>(null)
const stream = useChatStream()

/** 流式期间把答案区滚到底，否则用户看到的永远是第一段。 */
function scrollAnswerToEnd() {
  const el = answerBox.value
  if (el) el.scrollTop = el.scrollHeight
}

function reset() {
  answer.value = ''
  trace.value = []
  frames.value = []
  errMsg.value = ''
  intentInfo.value = null
  meta.value = {}
}

function onEvent(evt: StreamEvent) {
  switch (evt.event) {
    case 'intent': {
      const d = evt.data as { intent: IntentKind; confidence: number }
      intentInfo.value = { intent: d.intent, confidence: d.confidence }
      trace.value.push({
        kind: 'intent',
        label: `意图识别 · ${INTENT_LABEL[d.intent] ?? d.intent}`,
        detail: `置信度 ${(d.confidence * 100).toFixed(0)}%`,
      })
      break
    }
    case 'clarify': {
      const d = evt.data as { question: string }
      trace.value.push({ kind: 'clarify', label: '需要澄清', detail: d.question })
      break
    }
    case 'plan': {
      const d = evt.data as { steps: PlanStep[]; round?: number }
      trace.value.push({
        kind: 'plan',
        label: `规划 ${d.steps?.length ?? 0} 步`,
        detail: (d.steps || [])
          .map((s, i) => `${s.step ?? i + 1}. ${s.goal ?? ''}${s.tool ? `  [${s.tool}]` : ''}`)
          .join('\n'),
      })
      break
    }
    case 'tool': {
      const d = evt.data as { name: string; status: string; crag_level?: string; n?: number }
      if (!d?.name) break
      const last = trace.value.at(-1)
      const detail = d.crag_level ? `CRAG=${d.crag_level} · 命中 ${d.n ?? 0} 块` : undefined
      // 同名工具的 running → done 合并成一条，不然时间线会被刷屏
      if (last?.kind === 'tool' && last.label.startsWith(d.name) && d.status !== 'running') {
        last.label = `${d.name} · ${d.status}`
        if (detail) last.detail = detail
      } else {
        trace.value.push({ kind: 'tool', label: `${d.name} · ${d.status}`, detail })
      }
      break
    }
    case 'citation': {
      const c = evt.data as Citation
      trace.value.push({
        kind: 'citation',
        label: `引用 [${c.marker ?? '?'}]`,
        detail: c.verified ? '已通过蕴含校验' : '未校验',
      })
      meta.value.citations = (meta.value.citations ?? 0) + 1
      break
    }
    case 'reflection': {
      const d = evt.data as { round: number; verdict: string; overall?: number }
      trace.value.push({
        kind: 'reflection',
        label: `反思 第 ${d.round ?? 1} 轮 · ${d.verdict || '—'}`,
        detail: d.overall != null ? `综合得分 ${d.overall}` : undefined,
      })
      break
    }
    case 'replan': {
      const d = evt.data as { decision: string }
      trace.value.push({ kind: 'replan', label: '重规划', detail: d.decision })
      break
    }
    case 'guardrail': {
      const d = evt.data as { action: string; flags: string[] }
      trace.value.push({
        kind: 'guardrail',
        label: `防护 · ${d.action || '通过'}`,
        detail: (d.flags || []).join(' · ') || undefined,
      })
      break
    }
    case 'token':
      answer.value += (evt.data as { text: string }).text || ''
      nextTick(scrollAnswerToEnd)
      break
    case 'done': {
      const d = evt.data as { grounding_ratio?: number; latency_ms?: number; usage?: Record<string, number> }
      meta.value.grounding = d.grounding_ratio
      meta.value.latency = d.latency_ms
      meta.value.tokens = d.usage?.total_tokens
      break
    }
    case 'error': {
      const d = evt.data as { code: number; message: string }
      errMsg.value = `${d.message}（code ${d.code}）`
      break
    }
  }
}

async function ask(text?: string) {
  if (running.value) return
  const q = (text ?? question.value).trim()
  if (!q) return

  question.value = q
  reset()
  running.value = true

  await stream.start(
    { query: q, conversation_id: null, paper_ids: [] },
    {
      onEvent,
      onRawFrame: (frame) => {
        // 只留最多 60 条，避免长回答把 DOM 撑爆
        frames.value.push(frame)
        if (frames.value.length > 60) frames.value.shift()
      },
      onError: (err) => {
        errMsg.value = err.message
      },
      onClose: () => {
        running.value = false
      },
    },
  )
}

function stop() {
  stream.abort()
  running.value = false
}

/** 事件 → 时间线圆点的颜色。只用状态色，不引新彩色。 */
const DOT_TONE: Record<TraceItem['kind'], string> = {
  intent: 'bg-brand',
  plan: 'bg-brand',
  tool: 'bg-ink-4',
  citation: 'bg-ok',
  reflection: 'bg-warn',
  replan: 'bg-warn',
  guardrail: 'bg-bad',
  clarify: 'bg-warn',
}
</script>

<template>
  <section id="demo" class="mx-auto max-w-page px-5 sm:px-8">
    <MarketingReveal>
      <div class="mb-8 text-center">
        <h2 class="text-section font-medium text-ink">不是截图，是真的在跑</h2>
        <p class="mx-auto mt-3 max-w-text text-lead text-ink-2">
          下面这块直连后端 <code class="rounded bg-sunken px-1.5 py-0.5 font-mono text-[13px]">POST /chat/stream</code>，
          回答由 <span class="text-ink">SSE 逐 token 推回来</span>。右侧能同时看到 Agent 的调用链和原始协议帧。
        </p>
      </div>
    </MarketingReveal>

    <MarketingReveal :delay="80">
      <div class="overflow-hidden rounded-xl border border-hairline bg-surface shadow-sm">
        <!-- 输入区 -->
        <div class="border-b border-hairline p-4 sm:p-5">
          <div class="flex flex-wrap gap-2">
            <button
              v-for="p in PRESETS"
              :key="p"
              type="button"
              :disabled="running"
              class="rounded-full border border-hairline px-3 py-1.5 text-[12.5px] text-ink-2 transition-colors duration-[var(--dur-fast)] hover:border-brand-ring hover:bg-brand-soft hover:text-brand-ink disabled:cursor-not-allowed disabled:opacity-50"
              @click="ask(p)"
            >
              {{ p }}
            </button>
          </div>

          <div class="mt-3 flex items-end gap-2">
            <textarea
              v-model="question"
              rows="2"
              :disabled="running"
              placeholder="问点什么…（Enter 提问，Shift+Enter 换行）"
              class="min-h-[52px] flex-1 resize-none rounded-lg border border-hairline bg-canvas px-3 py-2.5 text-[13.5px] text-ink outline-none transition-colors duration-[var(--dur-fast)] placeholder:text-ink-4 focus:border-brand-ring disabled:opacity-60"
              @keydown.enter.exact.prevent="ask()"
            />
            <button
              v-if="running"
              type="button"
              class="inline-flex h-[52px] shrink-0 items-center gap-1.5 rounded-lg border border-hairline px-4 text-[13.5px] text-ink-2 transition-colors duration-[var(--dur-fast)] hover:bg-hover"
              @click="stop"
            >
              <PhStop :size="14" weight="fill" />
              停止
            </button>
            <button
              v-else
              type="button"
              :disabled="!question.trim()"
              class="inline-flex h-[52px] shrink-0 items-center gap-1.5 rounded-lg bg-brand px-4 text-[13.5px] font-medium text-white transition-colors duration-[var(--dur-fast)] hover:bg-brand-ink disabled:cursor-not-allowed disabled:opacity-40"
              @click="ask()"
            >
              提问
              <PhPaperPlaneRight :size="14" weight="fill" />
            </button>
          </div>

          <p v-if="intentInfo" class="mt-3 flex items-center gap-1.5 text-[12px] text-ink-3">
            <PhCrosshair :size="13" />
            识别为
            <span class="text-ink-2">{{ INTENT_LABEL[intentInfo.intent] ?? intentInfo.intent }}</span>
            · 置信度 {{ (intentInfo.confidence * 100).toFixed(0) }}%
          </p>
        </div>

        <!-- 输出区 -->
        <div class="grid lg:grid-cols-[1fr_320px]">
          <!-- 左：答案 -->
          <div class="min-w-0 p-4 sm:p-5">
            <div class="mb-2.5 flex items-center gap-2">
              <span
                class="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px]"
                :class="running ? 'bg-brand-soft text-brand-ink' : 'bg-sunken text-ink-3'"
              >
                <PhCircleNotch v-if="running" :size="11" class="animate-spin" />
                {{ running ? 'SSE 流式接收中' : '就绪' }}
              </span>
              <span v-if="meta.latency != null" class="text-[11px] text-ink-4">
                耗时 {{ meta.latency }}ms
              </span>
              <span v-if="meta.tokens != null" class="text-[11px] text-ink-4">
                · {{ meta.tokens }} tokens
              </span>
              <span v-if="meta.grounding != null" class="text-[11px] text-ink-4">
                · Grounding {{ (meta.grounding * 100).toFixed(0) }}%
              </span>
            </div>

            <div
              ref="answerBox"
              class="h-[300px] overflow-y-auto scroll-slim rounded-lg border border-hairline bg-canvas p-4"
            >
              <MarkdownView v-if="answer || running" :source="answer" :streaming="running" />
              <p v-else class="text-[13px] text-ink-4">
                点上面的问题，或自己写一个 —— 回答会一个字一个字长出来。
              </p>
            </div>

            <div
              v-if="errMsg"
              class="mt-3 flex items-start gap-2 rounded-lg bg-bad-soft px-3 py-2.5 text-[12.5px] text-bad"
            >
              <PhWarningCircle :size="15" class="mt-0.5 shrink-0" />
              <span class="break-all">{{ errMsg }}</span>
            </div>
            <p v-if="errMsg" class="mt-2 text-[12px] leading-relaxed text-ink-3">
              这台演示机是<strong class="font-medium text-ink-2">降级运行</strong>的：向量库与 PostgreSQL
              都没起来，Python 环境也没装
              <code class="rounded bg-canvas px-1 py-0.5 text-[11px]">transformers</code>，
              所以凡是需要检索论文的意图都会停在检索节点。意图识别、规划、反思、重规划这些不依赖
              外部服务的环节照常工作 —— 换成不需要检索的问题就能看到完整输出。
            </p>
          </div>

          <!-- 右：Agent 调用链 -->
          <div class="min-w-0 border-t border-hairline p-4 sm:p-5 lg:border-l lg:border-t-0">
            <h3 class="mb-3 text-[12px] font-medium uppercase tracking-wide text-ink-4">
              Agent 调用链
            </h3>

            <div class="h-[300px] overflow-y-auto scroll-slim">
              <ol v-if="trace.length" class="space-y-3">
                <li v-for="(t, i) in trace" :key="i" class="flex gap-2.5">
                  <span
                    class="mt-[5px] h-1.5 w-1.5 shrink-0 rounded-full"
                    :class="DOT_TONE[t.kind]"
                  />
                  <div class="min-w-0">
                    <p class="text-[12.5px] text-ink-2">{{ t.label }}</p>
                    <pre
                      v-if="t.detail"
                      class="mt-1 whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-ink-4"
                    >{{ t.detail }}</pre>
                  </div>
                </li>
              </ol>
              <p v-else class="text-[13px] text-ink-4">提问后这里会实时列出每一步。</p>
            </div>
          </div>
        </div>

        <!-- 原始 SSE 帧 -->
        <div class="border-t border-hairline">
          <button
            type="button"
            class="flex w-full items-center gap-2 px-4 py-3 text-left transition-colors duration-[var(--dur-fast)] hover:bg-hover sm:px-5"
            :aria-expanded="showFrames"
            aria-controls="raw-sse-frames"
            @click="showFrames = !showFrames"
          >
            <span class="text-[12.5px] font-medium text-ink-2">
              原始 SSE 帧
              <span v-if="frames.length" class="font-normal text-ink-4">（{{ frames.length }} 条）</span>
            </span>
            <PhCaretDown
              :size="14"
              class="ml-auto text-ink-3 transition-transform duration-[var(--dur-base)]"
              :class="showFrames ? 'rotate-180' : ''"
            />
          </button>

          <div
            v-show="showFrames"
            id="raw-sse-frames"
            class="max-h-[260px] overflow-y-auto scroll-slim border-t border-hairline bg-ink px-4 py-4 sm:px-5"
          >
            <pre
              v-if="frames.length"
              class="whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-ink-4"
            >{{ frames.join('\n\n') }}</pre>
            <p v-else class="font-mono text-[11px] text-ink-4">（等待提问）</p>
          </div>
        </div>
      </div>
    </MarketingReveal>
  </section>
</template>
