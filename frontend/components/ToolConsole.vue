<script setup lang="ts">
/**
 * MCP 工具直调台。
 *
 * 存在的意义：Agent 里的工具调用被包在 ReAct 循环里，出问题时很难看清是
 * "模型没选对工具"还是"工具本身坏了"。这个面板跳过模型直接打工具，
 * 把这两类问题分开。
 */
import { PhArrowsClockwise } from '@phosphor-icons/vue'
import type { ToolCallResult, ToolInfo } from '~/types/api'

const ui = useUiStore()
const api = useApi()

const selected = ref<ToolInfo | null>(null)
const argsText = ref('{}')
const calling = ref(false)
const result = ref<ToolCallResult | null>(null)
const errorMessage = ref('')

const grouped = computed(() => {
  const remote = ui.tools.filter((t) => t.kind === 'remote')
  const local = ui.tools.filter((t) => t.kind !== 'remote')
  return { remote, local }
})

function pick(tool: ToolInfo) {
  selected.value = tool
  result.value = null
  errorMessage.value = ''
  // 用 args_schema 里的必填项生成骨架，省得用户去翻文档
  const schema = (tool.args_schema || {}) as {
    properties?: Record<string, { type?: string; description?: string }>
    required?: string[]
  }
  const props = schema.properties || {}
  const skeleton: Record<string, unknown> = {}
  for (const [key, def] of Object.entries(props)) {
    skeleton[key] = def.type === 'number' || def.type === 'integer' ? 0 : def.type === 'array' ? [] : ''
  }
  argsText.value = JSON.stringify(skeleton, null, 2)
}

function prettySchema(tool: ToolInfo | null) {
  const schema = (tool?.args_schema || {}) as {
    properties?: Record<string, { type?: string; description?: string }>
    required?: string[]
  }
  const props = schema.properties || {}
  const required = new Set(schema.required || [])
  return Object.entries(props).map(([k, v]) => ({
    key: k,
    type: v.type || 'any',
    required: required.has(k),
    desc: v.description || '',
  }))
}

async function call() {
  if (!selected.value) return
  let args: Record<string, unknown>
  try {
    args = JSON.parse(argsText.value || '{}')
  } catch (err) {
    errorMessage.value = `参数不是合法 JSON：${(err as Error).message}`
    return
  }

  calling.value = true
  errorMessage.value = ''
  result.value = null
  try {
    result.value = await api.post<ToolCallResult>('/tools/call', {
      name: selected.value.name,
      args,
    })
  } catch (err) {
    errorMessage.value = (err as Error).message
  } finally {
    calling.value = false
  }
}

const resultText = computed(() => {
  if (!result.value) return ''
  const r = result.value.result
  return typeof r === 'string' ? r : JSON.stringify(r, null, 2)
})

onMounted(() => void ui.loadTools())
</script>

<template>
  <div class="console">
    <div class="list rc-panel">
      <div class="rc-panel-head">
        <b class="rc-panel-title rc-grow">可用工具</b>
        <button
          class="rc-btn rc-btn--ghost rc-btn--icon rc-btn--sm"
          type="button"
          title="刷新工具清单"
          :disabled="ui.loadingTools"
          @click="ui.loadTools()"
        >
          <PhArrowsClockwise :size="13" :class="{ spin: ui.loadingTools }" />
        </button>
      </div>

      <div class="list-body rc-scroll">
        <p v-if="ui.toolsNote" class="rc-alert rc-alert--warn note">{{ ui.toolsNote }}</p>

        <div class="group">
          <div class="group-title">MCP Server（{{ grouped.remote.length }}）</div>
          <button
            v-for="t in grouped.remote"
            :key="t.name"
            type="button"
            class="tool"
            :class="{ active: selected?.name === t.name }"
            @click="pick(t)"
          >
            <span class="rc-truncate tool-name">{{ t.name }}</span>
            <span class="tool-desc">{{ t.description || '（无描述）' }}</span>
          </button>
          <p v-if="!grouped.remote.length" class="rc-muted tool-empty">
            没有远程工具。检查 .env 里的 MCP_*_ENABLED 与各 Server 是否启动成功。
          </p>
        </div>

        <div class="group">
          <div class="group-title">进程内原生工具（{{ grouped.local.length }}）</div>
          <button
            v-for="t in grouped.local"
            :key="t.name"
            type="button"
            class="tool"
            :class="{ active: selected?.name === t.name }"
            @click="pick(t)"
          >
            <span class="rc-truncate tool-name">{{ t.name }}</span>
            <span class="tool-desc">不走 MCP 协议，直接调内部实现</span>
          </button>
        </div>
      </div>

      <div class="list-foot rc-caption">已启用 Server：{{ ui.enabledServers.join(' / ') || '—' }}</div>
    </div>

    <div class="detail rc-panel">
      <template v-if="selected">
        <div class="rc-panel-head">
          <b class="rc-panel-title rc-grow">{{ selected.name }}</b>
          <span class="rc-pill" :class="selected.kind === 'remote' ? 'rc-pill--primary' : 'rc-pill--dim'">
            {{ selected.kind === 'remote' ? selected.server || 'MCP' : 'in-process' }}
          </span>
          <button class="rc-btn rc-btn--primary rc-btn--sm" type="button" :disabled="calling" @click="call">
            <span v-if="calling" class="rc-spin" />
            调用
          </button>
        </div>

        <div class="detail-body rc-scroll">
          <p class="rc-muted desc">{{ selected.description || '（该工具没有提供描述）' }}</p>

          <div v-if="prettySchema(selected).length" class="schema">
            <div class="block-title">参数</div>
            <div v-for="f in prettySchema(selected)" :key="f.key" class="field">
              <span class="rc-mono">{{ f.key }}</span>
              <span class="rc-pill rc-pill--dim">{{ f.type }}</span>
              <span v-if="f.required" class="rc-pill rc-pill--bad">必填</span>
              <span class="rc-muted rc-grow">{{ f.desc }}</span>
            </div>
          </div>

          <div class="block-title" style="margin-top: 10px">参数（JSON）</div>
          <textarea v-model="argsText" class="rc-textarea rc-mono" rows="7" spellcheck="false" />

          <p v-if="errorMessage" class="rc-alert rc-alert--bad" style="margin-top: 10px">{{ errorMessage }}</p>

          <template v-if="result">
            <div class="block-title" style="margin-top: 12px">
              返回
              <span class="rc-pill" :class="result.ok ? 'rc-pill--ok' : 'rc-pill--bad'">
                {{ result.ok ? 'ok' : '出错' }}
              </span>
              <span class="rc-pill rc-pill--dim">{{ result.latency_ms }} ms</span>
            </div>
            <pre class="result rc-scroll">{{ resultText }}</pre>
          </template>
        </div>
      </template>

      <div v-else class="rc-empty">从左侧选一个工具开始</div>
    </div>
  </div>
</template>

<style scoped>
.console {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 12px;
  height: calc(100vh - 96px);
}

.list {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.list-body {
  flex: 1;
}
.list-foot {
  padding: 7px 11px;
  border-top: 1px solid var(--rc-hairline);
}

.note {
  margin: 6px 8px 0;
}

.group {
  padding: 4px 0 8px;
}
.group-title {
  padding: 6px 11px 3px;
  font-size: 11.5px;
  color: var(--rc-ink-tertiary);
}

.tool {
  display: block;
  width: 100%;
  padding: 6px 11px;
  border: none;
  border-left: 2px solid transparent;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.tool:hover {
  background: var(--rc-surface-2);
}
.tool.active {
  background: var(--rc-primary-soft);
  border-left-color: var(--rc-primary);
}
.tool-name {
  display: block;
  font-size: 13px;
  font-weight: 500;
}
.tool-desc {
  display: block;
  font-size: 11.5px;
  line-height: 1.45;
  color: var(--rc-ink-subtle);
  max-height: 34px;
  overflow: hidden;
}
.tool-empty {
  padding: 6px 11px;
  font-size: 12px;
}

.detail {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.detail-body {
  flex: 1;
  padding: 11px;
}
.desc {
  margin: 0 0 10px;
  font-size: 12.5px;
}

.block-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 5px;
  font-family: var(--rc-font-display);
  font-weight: 600;
  font-size: 12.5px;
  color: var(--rc-ink);
}

.field {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 0;
  font-size: 12px;
}
.field .rc-muted {
  font-size: 11.5px;
}

.result {
  margin: 0;
  padding: 11px;
  background: var(--rc-canvas);
  color: var(--rc-ink-muted);
  border: 1px solid var(--rc-hairline);
  border-radius: var(--rc-radius-md);
  font-size: 12px;
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

.spin {
  animation: rc-rotate 0.7s linear infinite;
}
</style>
