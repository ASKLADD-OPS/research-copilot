<script setup lang="ts">
/**
 * 主题探索：给一个主题，Agent 自己走完「规划检索式 → 两路检索 → 相关性打分 →
 * 下载入库 → 评审 → 换词重来 → 推荐」，执行轨迹逐步显示。
 *
 * 为什么是独立路由而不是工作台里的一个面板：这条闭环可能跑几分钟（要下 PDF、
 * 解析、算向量），而工作台的三栏是"看着原文提问"的空间，塞一条长流水进去会把
 * 阅读区挤没。探索是一次性任务，有自己的开始与结束，独立页面才对得上它的生命周期。
 *
 * 事件协议见 `backend/app/agents/explore.py` 的模块头：thought / action /
 * observation / progress / recommend / done / error。本页按事件名分派。
 */
import {
  PhArrowLeft,
  PhArrowsClockwise,
  PhBell,
  PhCheckCircle,
  PhClock,
  PhEye,
  PhLightbulb,
  PhMagnifyingGlass,
  PhPlay,
  PhSparkle,
  PhStar,
  PhStop,
  PhTrash,
  PhWarningCircle,
  PhWrench,
} from '@phosphor-icons/vue'
import type {
  ExploreDone,
  Recommendation,
  SchedulerStatus,
  StreamEvent,
  Subscription,
} from '~/types/api'

definePageMeta({ layout: false })

useHead({
  title: '主题探索 · Research Copilot',
  meta: [{ name: 'robots', content: 'noindex, nofollow' }],
})

const api = useApi()
const stream = useChatStream()

// ------------------------------------------------------------------ 参数
const topic = ref('')
const maxPapers = ref(8)
const minScore = ref(0.7)
const maxRounds = ref(3)

// ------------------------------------------------------------------ 运行态
/** 轨迹的一行。progress / recommend / done 不进这里 —— 它们各有各的位置。 */
interface TraceItem {
  kind: 'thought' | 'action' | 'observation'
  round: number
  stage?: string
  name?: string
  args?: Record<string, unknown> | null
  ok?: boolean
  text: string
}

const running = ref(false)
const runningTopic = ref('')
const errorMessage = ref('')
const trace = ref<TraceItem[]>([])
const progress = ref({ stage: '', index: 0, total: 6, round: 1, detail: '' })
const recommendations = ref<Recommendation[]>([])
const done = ref<ExploreDone | null>(null)

const STAGE_LABEL: Record<string, string> = {
  plan: '制定检索式',
  search: '两路检索',
  score: '相关性打分',
  download: '下载入库',
  reflect: '结果评审',
  recommend: '生成推荐',
}

const pct = computed(() =>
  done.value ? 100 : Math.round((progress.value.index / Math.max(1, progress.value.total)) * 100),
)

const downloadedCount = computed(() => recommendations.value.filter((r) => r.downloaded).length)

// ------------------------------------------------------------------ 订阅
const subscriptions = ref<Subscription[]>([])
const scheduler = ref<SchedulerStatus | null>(null)
const loadingSubs = ref(false)
const subError = ref('')

// ------------------------------------------------------------------ 事件分派
function onEvent(evt: StreamEvent) {
  const d = (evt.data ?? {}) as Record<string, any>
  switch (evt.event) {
    case 'thought':
      trace.value.push({ kind: 'thought', round: d.round ?? 1, stage: d.stage, text: d.text ?? '' })
      break
    case 'action':
      trace.value.push({ kind: 'action', round: d.round ?? 1, name: d.name, args: d.args ?? null, text: '' })
      break
    case 'observation':
      trace.value.push({ kind: 'observation', round: 0, name: d.name, ok: d.ok !== false, text: d.text ?? '' })
      break
    case 'progress':
      progress.value = {
        stage: d.stage ?? '',
        index: d.index ?? 0,
        total: d.total ?? 6,
        round: d.round ?? 1,
        detail: d.detail ?? '',
      }
      break
    case 'recommend':
      // 逐条到达，让结果"边跑边出现"；done 帧到达时再用权威列表覆盖一次
      recommendations.value.push(d as Recommendation)
      break
    case 'done':
      done.value = d as ExploreDone
      if (Array.isArray(d.recommendations)) recommendations.value = d.recommendations
      break
    case 'error':
      errorMessage.value = d.message || '探索失败'
      break
  }
}

function reset() {
  errorMessage.value = ''
  trace.value = []
  recommendations.value = []
  done.value = null
  progress.value = { stage: '', index: 0, total: 6, round: 1, detail: '' }
}

function run(path: string, body: unknown) {
  if (running.value) return
  reset()
  running.value = true
  void stream.start(
    body,
    {
      onEvent,
      onError: (e) => {
        errorMessage.value = e.message
        running.value = false
      },
      onClose: () => {
        running.value = false
      },
    },
    path,
  )
}

function exploreTopic() {
  const t = topic.value.trim()
  if (!t) {
    errorMessage.value = '先给一个研究主题。'
    return
  }
  runningTopic.value = t
  run('/tools/explore', {
    topic: t,
    max_papers: maxPapers.value,
    min_score: minScore.value,
    max_rounds: maxRounds.value,
  })
}

/** 手动触发一次订阅抓取。走的是与定时抓取完全相同的闭环，所以轨迹是同一套。 */
function runSubscription(sub: Subscription) {
  runningTopic.value = sub.topic
  run(`/tools/subscriptions/${sub.id}/run`, {})
}

function stop() {
  stream.abort()
  running.value = false
}

// ------------------------------------------------------------------ 订阅 CRUD
async function loadSubscriptions() {
  loadingSubs.value = true
  try {
    subscriptions.value = await api.get<Subscription[]>('/tools/subscriptions')
    subError.value = ''
  } catch (err) {
    subError.value = (err as Error).message
  } finally {
    loadingSubs.value = false
  }
}

async function loadScheduler() {
  try {
    scheduler.value = await api.get<SchedulerStatus>('/tools/scheduler')
  } catch {
    // 调度状态是锦上添花，拿不到就不显示，不打扰用户
  }
}

async function subscribeTopic() {
  const t = topic.value.trim()
  if (!t) {
    errorMessage.value = '先给一个研究主题。'
    return
  }
  try {
    await api.post('/tools/subscribe', {
      topic: t,
      max_papers: maxPapers.value,
      min_score: minScore.value,
    })
    await loadSubscriptions()
  } catch (err) {
    subError.value = (err as Error).message
  }
}

async function removeSubscription(id: number) {
  try {
    await api.del(`/tools/subscriptions/${id}`)
    await loadSubscriptions()
  } catch (err) {
    subError.value = (err as Error).message
  }
}

function pad(n: number) {
  return String(n).padStart(2, '0')
}

function scoreTone(score: number) {
  if (score >= 0.8) return 'ok'
  if (score >= 0.7) return 'brand'
  return 'warn'
}

onMounted(() => {
  void loadSubscriptions()
  void loadScheduler()
})

onBeforeUnmount(() => stream.abort())
</script>

<template>
  <div class="flex h-screen flex-col bg-canvas text-[12.5px] text-ink">
    <!-- ---------------------------------------------------------------- 顶栏 -->
    <header class="flex h-11 shrink-0 items-center gap-2 border-b border-hairline bg-surface px-3">
      <NuxtLink
        to="/app"
        class="inline-flex h-6.5 shrink-0 items-center gap-1 rounded-md px-2 text-[11.5px] text-ink-3 transition-colors hover:bg-hover hover:text-ink"
      >
        <PhArrowLeft :size="11" />
        工作台
      </NuxtLink>
      <span class="mx-1 h-4 w-px shrink-0 bg-hairline" />
      <span class="grid size-5 shrink-0 place-items-center rounded-[6px] bg-brand text-white">
        <PhSparkle :size="12" weight="fill" />
      </span>
      <span class="text-[13px] font-semibold tracking-tight">主题探索</span>
      <span class="min-w-0 flex-1" />
      <span
        v-if="scheduler"
        class="inline-flex shrink-0 items-center gap-1.5 text-2xs text-ink-3"
        :title="scheduler.engine"
      >
        <PhClock :size="11" />
        <template v-if="scheduler.enabled">
          每天 {{ pad(scheduler.hour) }}:{{ pad(scheduler.minute) }} 自动抓取
        </template>
        <template v-else>定时抓取已关闭</template>
      </span>
    </header>

    <div class="grid min-h-0 flex-1 grid-cols-[360px_1fr]">
      <!-- ------------------------------------------------------------ 左栏 -->
      <aside class="flex min-h-0 flex-col border-r border-hairline bg-surface">
        <!-- 参数 -->
        <div class="shrink-0 space-y-2.5 border-b border-hairline px-3 py-3">
          <div :class="FIELD_CLS">
            <label :class="LABEL_CLS" for="explore-topic">研究主题</label>
            <textarea
              id="explore-topic"
              v-model="topic"
              :class="TEXTAREA_CLS"
              rows="3"
              placeholder="如：MoE 专家路由的负载均衡"
              @keydown.meta.enter="exploreTopic"
              @keydown.ctrl.enter="exploreTopic"
            />
          </div>

          <div class="grid grid-cols-3 gap-2">
            <div :class="FIELD_CLS">
              <label :class="LABEL_CLS" for="explore-max">最多篇数</label>
              <input id="explore-max" v-model.number="maxPapers" :class="INPUT_CLS" type="number" min="1" max="20" />
            </div>
            <div :class="FIELD_CLS">
              <label :class="LABEL_CLS" for="explore-score">相关阈值</label>
              <input
                id="explore-score"
                v-model.number="minScore"
                :class="INPUT_CLS"
                type="number"
                min="0"
                max="1"
                step="0.05"
              />
            </div>
            <div :class="FIELD_CLS">
              <label :class="LABEL_CLS" for="explore-rounds">轮次上限</label>
              <input id="explore-rounds" v-model.number="maxRounds" :class="INPUT_CLS" type="number" min="1" max="5" />
            </div>
          </div>

          <div class="flex gap-1.5">
            <button
              type="button"
              :class="btnCls('primary', { block: true })"
              :disabled="running || !topic.trim()"
              @click="exploreTopic"
            >
              <AppSpinner v-if="running" :size="11" />
              <PhMagnifyingGlass v-else :size="11" weight="bold" />
              开始探索
            </button>
            <button type="button" :class="btnCls('default')" title="停止当前探索" :disabled="!running" @click="stop">
              <PhStop :size="10" weight="fill" />
              停止
            </button>
          </div>

          <button
            type="button"
            :class="btnCls('default', { block: true })"
            :disabled="!topic.trim()"
            title="每天固定时间自动抓一遍这个主题"
            @click="subscribeTopic"
          >
            <PhBell :size="11" />
            订阅这个主题
          </button>
        </div>

        <!-- 订阅列表 -->
        <div class="flex shrink-0 items-center gap-1.5 border-b border-hairline px-3 py-1.5">
          <PhBell :size="12" class="text-ink-3" />
          <span class="text-2xs font-semibold text-ink-2">
            订阅 <span class="font-normal text-ink-4">{{ subscriptions.length }}</span>
          </span>
          <span class="min-w-0 flex-1" />
          <button
            type="button"
            :class="iconBtnCls('sm')"
            :disabled="loadingSubs"
            title="刷新订阅列表"
            @click="loadSubscriptions"
          >
            <PhArrowsClockwise :size="11" :class="{ 'animate-spin': loadingSubs }" />
          </button>
        </div>

        <div class="min-h-0 flex-1 overflow-y-auto scroll-slim px-3 py-2">
          <p v-if="subError" class="mb-2 rounded-md bg-bad-soft px-2 py-1.5 text-2xs text-bad">
            {{ subError }}
          </p>

          <ul v-if="subscriptions.length" class="space-y-1.5">
            <li
              v-for="s in subscriptions"
              :key="s.id"
              class="rounded-lg border border-hairline px-2.5 py-2"
              :class="s.enabled ? '' : 'opacity-60'"
            >
              <div class="flex items-start gap-1.5">
                <span class="min-w-0 flex-1 truncate text-[12px] font-medium text-ink" :title="s.topic">
                  {{ s.topic }}
                </span>
                <span v-if="s.last_new > 0" :class="pillCls('ok')" title="上次抓取新入库的篇数">
                  +{{ s.last_new }}
                </span>
              </div>
              <p class="mt-0.5 flex items-center gap-1.5 text-2xs text-ink-4">
                <span :class="pillCls(s.last_status === 'failed' ? 'bad' : s.last_status === 'ok' ? 'ok' : 'default')">
                  {{ s.last_status }}
                </span>
                <span v-if="s.last_run_at">{{ formatTime(s.last_run_at) }}</span>
                <span v-else>尚未运行</span>
                <span class="ml-auto" :title="`累计新增 ${s.total_new} 篇`">累计 {{ s.total_new }}</span>
              </p>
              <p v-if="s.last_error" class="mt-1 line-clamp-2 text-2xs text-bad">{{ s.last_error }}</p>
              <div class="mt-1.5 flex gap-1.5">
                <button
                  type="button"
                  :class="btnCls('default', { size: 'sm' })"
                  :disabled="running"
                  title="不等定时，立刻抓一次"
                  @click="runSubscription(s)"
                >
                  <PhPlay :size="9" weight="fill" />
                  立即抓取
                </button>
                <button
                  type="button"
                  :class="btnCls('danger', { size: 'sm' })"
                  title="取消订阅"
                  @click="removeSubscription(s.id)"
                >
                  <PhTrash :size="10" />
                </button>
              </div>
            </li>
          </ul>

          <div v-else-if="!loadingSubs" :class="EMPTY_CLS">
            <PhBell :size="18" class="mb-1 text-ink-4" />
            <strong class="text-[12.5px] text-ink-2">还没有订阅</strong>
            <span class="text-2xs leading-relaxed text-ink-4">
              填好主题后点「订阅这个主题」，之后每天到点自动抓一遍，新论文直接进文献库。
            </span>
          </div>
        </div>
      </aside>

      <!-- ------------------------------------------------------------ 右栏 -->
      <main class="flex min-h-0 flex-col">
        <!-- 进度 -->
        <div class="shrink-0 border-b border-hairline bg-surface px-4 py-2.5">
          <div class="mb-1.5 flex items-center gap-2">
            <span class="text-[12px] font-medium text-ink-2">
              {{ runningTopic ? runningTopic : '尚未开始' }}
            </span>
            <span v-if="progress.stage" :class="pillCls('brand')">
              {{ STAGE_LABEL[progress.stage] ?? progress.stage }}
            </span>
            <span v-if="progress.round > 1" :class="pillCls('default')">第 {{ progress.round }} 轮</span>
            <span class="min-w-0 flex-1" />
            <span v-if="done" class="text-2xs text-ink-4">
              {{ done.rounds }} 轮 · {{ done.searched }} 候选 · 入库 {{ done.downloaded }} · 达标 {{ done.passed }}
              · 质量 {{ done.quality.toFixed(2) }} · {{ (done.elapsed_ms / 1000).toFixed(1) }}s
            </span>
            <span v-else-if="progress.detail" class="text-2xs text-ink-4">{{ progress.detail }}</span>
          </div>
          <div class="h-1 overflow-hidden rounded-full bg-sunken">
            <div
              class="h-full rounded-full transition-all duration-300"
              :class="errorMessage ? 'bg-bad' : done ? 'bg-ok' : 'bg-brand'"
              :style="{ width: `${pct}%` }"
            />
          </div>
        </div>

        <p v-if="errorMessage" class="shrink-0 border-b border-hairline bg-bad-soft px-4 py-2 text-2xs text-bad">
          {{ errorMessage }}
        </p>

        <!-- 轨迹 -->
        <div class="min-h-0 flex-1 overflow-y-auto scroll-slim px-4 py-3">
          <template v-if="trace.length">
            <ol class="space-y-1">
              <li v-for="(t, i) in trace" :key="i">
                <!-- Thought -->
                <div v-if="t.kind === 'thought'" class="flex items-start gap-2 py-0.5">
                  <PhLightbulb :size="12" class="mt-0.5 shrink-0 text-brand" />
                  <span class="shrink-0 text-2xs font-medium text-ink-4">思考</span>
                  <span class="min-w-0 flex-1 text-[12px] leading-relaxed text-ink-2">{{ t.text }}</span>
                </div>

                <!-- Action -->
                <div v-else-if="t.kind === 'action'" class="flex items-start gap-2 py-0.5">
                  <PhWrench :size="12" class="mt-0.5 shrink-0 text-ink-3" />
                  <span class="shrink-0 text-2xs font-medium text-ink-4">调用</span>
                  <span class="min-w-0 flex-1">
                    <span :class="MONO_CLS" class="text-ink">{{ t.name }}</span>
                    <span v-if="t.args" :class="MONO_CLS" class="ml-1.5 text-ink-4">
                      {{ JSON.stringify(t.args) }}
                    </span>
                  </span>
                </div>

                <!-- Observation -->
                <div v-else class="flex items-start gap-2 py-0.5">
                  <PhCheckCircle v-if="t.ok" :size="12" class="mt-0.5 shrink-0 text-ok" />
                  <PhWarningCircle v-else :size="12" class="mt-0.5 shrink-0 text-warn" />
                  <span class="shrink-0 text-2xs font-medium text-ink-4">结果</span>
                  <span class="min-w-0 flex-1 text-[12px] leading-relaxed" :class="t.ok ? 'text-ink-2' : 'text-warn'">
                    {{ t.text }}
                  </span>
                </div>
              </li>
            </ol>
          </template>

          <div v-else-if="running" :class="EMPTY_CLS">
            <AppSpinner :size="18" class="mb-1 text-brand" />
            <strong class="text-[12.5px] text-ink-2">Agent 正在跑第一轮</strong>
            <span class="text-2xs text-ink-4">轨迹会随着每一步实时出现。</span>
          </div>

          <div v-else :class="EMPTY_CLS">
            <PhMagnifyingGlass :size="18" class="mb-1 text-ink-4" />
            <strong class="text-[12.5px] text-ink-2">给一个主题，让 Agent 自己去翻</strong>
            <span class="text-2xs leading-relaxed text-ink-4">
              它会规划检索式、跑 arXiv 与 Semantic Scholar 两路、按阈值打分、把达标的下下来，
              再由评审决定要不要换词重来。每一步的思考、调用与结果都会出现在这里。
            </span>
          </div>
        </div>

        <!-- 推荐 -->
        <section
          v-if="recommendations.length"
          class="flex max-h-[44%] min-h-0 shrink-0 flex-col border-t border-hairline bg-surface"
        >
          <div class="flex shrink-0 items-center gap-1.5 border-b border-hairline px-4 py-1.5">
            <PhStar :size="12" class="text-brand" weight="fill" />
            <span class="text-2xs font-semibold text-ink-2">推荐</span>
            <span class="text-2xs text-ink-4">
              {{ recommendations.length }} 篇 · {{ downloadedCount }} 篇已入库
            </span>
            <span class="min-w-0 flex-1" />
            <span class="text-2xs text-ink-4">阈值 ≥ {{ done?.min_score ?? minScore }}</span>
          </div>
          <div class="min-h-0 flex-1 overflow-y-auto scroll-slim px-4 py-2">
            <ul class="space-y-1.5">
              <li
                v-for="(r, i) in recommendations"
                :key="`${r.arxiv_id || r.title}-${i}`"
                class="rounded-lg border border-hairline px-2.5 py-2"
              >
                <div class="flex items-start gap-2">
                  <span :class="pillCls(scoreTone(r.score))" class="mt-0.5 tabular-nums">
                    {{ r.score.toFixed(2) }}
                  </span>
                  <span class="min-w-0 flex-1 text-[12px] font-medium leading-snug text-ink">{{ r.title }}</span>
                  <span v-if="r.downloaded" :class="pillCls('ok')" class="mt-0.5">已入库</span>
                </div>
                <p class="mt-0.5 text-2xs text-ink-4">
                  <span v-if="r.authors.length">{{ r.authors.slice(0, 3).join(', ') }}{{ r.authors.length > 3 ? ' 等' : '' }} · </span>
                  <span v-if="r.year">{{ r.year }} · </span>
                  <span v-if="r.venue">{{ r.venue }} · </span>
                  <span>{{ r.source }}</span>
                  <span v-if="r.citation_count"> · 被引 {{ r.citation_count }}</span>
                </p>
                <p v-if="r.abstract" class="mt-1 line-clamp-2 text-2xs leading-relaxed text-ink-3">{{ r.abstract }}</p>
                <p class="mt-1 flex items-center gap-2 text-2xs">
                  <NuxtLink
                    v-if="r.paper_id"
                    :to="`/app?paper=${r.paper_id}`"
                    class="text-brand-ink hover:underline"
                  >
                    打开原文
                  </NuxtLink>
                  <a
                    v-else-if="r.url"
                    :href="r.url"
                    target="_blank"
                    rel="noopener"
                    class="text-brand-ink hover:underline"
                  >
                    查看来源
                  </a>
                  <span v-if="r.note" class="text-ink-4">{{ r.note }}</span>
                </p>
              </li>
            </ul>
          </div>
        </section>
      </main>
    </div>
  </div>
</template>
