<script setup lang="ts">
/**
 * 工具台（纯内容）：跳过 Agent 直接打 MCP 工具。
 *
 * 存在的意义：工具调用平时被包在 ReAct 循环里，出问题时很难分清是
 * "模型没选对工具"还是"工具本身坏了"。这个面板把这两类问题分开。
 */
import { PhArrowsClockwise, PhCaretLeft, PhPlay, PhWrench } from '@phosphor-icons/vue'
import type { ToolCallResult, ToolInfo } from '~/types/api'

const ui = useUiStore()

const selected = ref<ToolInfo | null>(null)
const argsText = ref('{}')
const calling = ref(false)
const result = ref<ToolCallResult | null>(null)
const errorMessage = ref('')

const grouped = computed(() => ({
  remote: ui.tools.filter((t) => t.kind === 'remote'),
  local: ui.tools.filter((t) => t.kind !== 'remote'),
}))

type SchemaShape = {
  properties?: Record<string, { type?: string; description?: string }>
  required?: string[]
}

function schemaOf(tool: ToolInfo | null): SchemaShape {
  return (tool?.args_schema ?? {}) as SchemaShape
}

function pick(tool: ToolInfo) {
  selected.value = tool
  result.value = null
  errorMessage.value = ''
  // 用 args_schema 生成骨架，省得用户去翻文档
  const shape = schemaOf(tool)
  const skeleton: Record<string, unknown> = {}
  for (const [key, def] of Object.entries(shape.properties ?? {})) {
    skeleton[key] = def.type === 'number' || def.type === 'integer' ? 0 : def.type === 'array' ? [] : ''
  }
  argsText.value = JSON.stringify(skeleton, null, 2)
}

const fields = computed(() => {
  const shape = schemaOf(selected.value)
  const required = new Set(shape.required ?? [])
  return Object.entries(shape.properties ?? {}).map(([key, def]) => ({
    key,
    type: def.type || 'any',
    required: required.has(key),
    desc: def.description || '',
  }))
})

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
    result.value = await useApi().post<ToolCallResult>('/tools/call', {
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
  <div class="flex h-full flex-col">
    <!-- 工具清单 -->
    <div class="flex shrink-0 items-center gap-1.5 border-b border-hairline px-2.5 py-1.5">
      <PhWrench :size="12" class="text-ink-3" />
      <span class="text-2xs font-semibold text-ink-2">
        可用工具 <span class="font-normal text-ink-4">{{ ui.tools.length }}</span>
      </span>
      <span class="min-w-0 flex-1" />
      <button
        type="button"
        :class="iconBtnCls('sm')"
        :disabled="ui.loadingTools"
        title="刷新工具清单"
        @click="ui.loadTools()"
      >
        <PhArrowsClockwise :size="11" :class="{ 'animate-spin': ui.loadingTools }" />
      </button>
    </div>

    <div class="max-h-[38%] shrink-0 overflow-y-auto scroll-slim border-b border-hairline">
      <p v-if="ui.toolsNote" class="m-2 rounded-md bg-warn-soft px-2 py-1.5 text-2xs text-warn">
        {{ ui.toolsNote }}
      </p>

      <div class="py-1">
        <p class="px-2.5 py-0.5 text-2xs text-ink-4">MCP Server（{{ grouped.remote.length }}）</p>
        <button
          v-for="t in grouped.remote"
          :key="t.name"
          type="button"
          class="block w-full border-l-2 px-2.5 py-1 text-left transition-colors"
          :class="selected?.name === t.name ? 'border-brand bg-brand-soft' : 'border-transparent hover:bg-hover'"
          @click="pick(t)"
        >
          <span class="block truncate font-mono text-[11.5px] font-medium text-ink-2">{{ t.name }}</span>
          <span class="line-clamp-1 block text-2xs text-ink-4">{{ t.description || '（无描述）' }}</span>
        </button>
        <p v-if="!grouped.remote.length" class="px-2.5 py-1 text-2xs text-ink-4">
          没有远程工具。检查 .env 里的 MCP_*_ENABLED 与各 Server 是否启动成功。
        </p>
      </div>

      <div class="py-1">
        <p class="px-2.5 py-0.5 text-2xs text-ink-4">进程内原生工具（{{ grouped.local.length }}）</p>
        <button
          v-for="t in grouped.local"
          :key="t.name"
          type="button"
          class="block w-full border-l-2 px-2.5 py-1 text-left transition-colors"
          :class="selected?.name === t.name ? 'border-brand bg-brand-soft' : 'border-transparent hover:bg-hover'"
          @click="pick(t)"
        >
          <span class="block truncate font-mono text-[11.5px] font-medium text-ink-2">{{ t.name }}</span>
          <span class="line-clamp-1 block text-2xs text-ink-4">不走 MCP 协议，直接调内部实现</span>
        </button>
      </div>

      <p class="border-t border-hairline px-2.5 py-1 text-2xs text-ink-4">
        已启用：{{ ui.enabledServers.join(' / ') || '—' }}
      </p>
    </div>

    <!-- 详情 -->
    <div class="min-h-0 flex-1 overflow-y-auto scroll-slim px-2.5 py-2.5">
      <template v-if="selected">
        <div class="mb-2 flex items-center gap-1.5">
          <button type="button" :class="iconBtnCls('sm')" title="返回列表" @click="selected = null">
            <PhCaretLeft :size="11" />
          </button>
          <span class="min-w-0 flex-1 truncate font-mono text-[11.5px] font-semibold text-ink">
            {{ selected.name }}
          </span>
          <span :class="pillCls(selected.kind === 'remote' ? 'brand' : 'default')">
            {{ selected.kind === 'remote' ? selected.server || 'MCP' : 'in-process' }}
          </span>
        </div>

        <p class="mb-2 text-2xs leading-relaxed text-ink-3">
          {{ selected.description || '（该工具没有提供描述）' }}
        </p>

        <div v-if="fields.length" class="mb-2">
          <p class="mb-1 text-2xs font-medium text-ink-3">参数</p>
          <ul class="space-y-0.5">
            <li v-for="f in fields" :key="f.key" class="flex items-start gap-1.5 text-2xs">
              <span class="shrink-0 font-mono text-ink-2">{{ f.key }}</span>
              <span :class="pillCls('default')">{{ f.type }}</span>
              <span v-if="f.required" :class="pillCls('bad')">必填</span>
              <span class="min-w-0 flex-1 text-ink-4">{{ f.desc }}</span>
            </li>
          </ul>
        </div>

        <p class="mb-1 text-2xs font-medium text-ink-3">参数（JSON）</p>
        <textarea
          v-model="argsText"
          :class="TEXTAREA_CLS"
          class="font-mono text-[11.5px]"
          rows="7"
          spellcheck="false"
        />

        <button
          type="button"
          :class="btnCls('primary', { block: true })"
          class="mt-2"
          :disabled="calling"
          @click="call"
        >
          <AppSpinner v-if="calling" :size="11" />
          <PhPlay v-else :size="10" weight="fill" />
          调用
        </button>

        <p v-if="errorMessage" class="mt-2 rounded-md bg-bad-soft px-2 py-1.5 text-2xs text-bad">
          {{ errorMessage }}
        </p>

        <template v-if="result">
          <p class="mt-2.5 mb-1 flex items-center gap-1.5 text-2xs font-medium text-ink-3">
            返回
            <span :class="pillCls(result.ok ? 'ok' : 'bad')">{{ result.ok ? 'ok' : '出错' }}</span>
            <span :class="pillCls('default')">{{ result.latency_ms }} ms</span>
          </p>
          <pre class="max-h-72 overflow-auto scroll-slim rounded-md border border-hairline bg-sunken p-2.5 font-mono text-[11.5px] whitespace-pre-wrap text-ink-2">{{ resultText }}</pre>
        </template>
      </template>

      <div v-else :class="EMPTY_CLS">
        <PhWrench :size="18" class="mb-1 text-ink-4" />
        <strong class="text-[12.5px] text-ink-2">选一个工具</strong>
        <span class="text-2xs leading-relaxed text-ink-4">
          参数会按 schema 生成骨架，改完直接调用。返回值原样展示，方便定位是工具坏了还是模型选错了。
        </span>
      </div>
    </div>
  </div>
</template>
