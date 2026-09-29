<script setup lang="ts">
/** 工具页：MCP 工具直调台 + 后端健康明细。 */
import { PhArrowsClockwise } from '@phosphor-icons/vue'

const ui = useUiStore()
onMounted(() => void ui.refreshHealth())
</script>

<template>
  <div class="rc-page">
    <header class="rc-page-head">
      <h1>工具（MCP）</h1>
      <p>
        所有外部能力（arXiv / PubMed / Semantic Scholar / Python 沙箱 / Web Search）
        都封装成独立的 FastMCP Server，经 MultiServerMCPClient 注册后供 Agent 调用。
        这里可以绕过模型直接打工具，用来区分「工具坏了」和「模型选错了工具」。
      </p>
    </header>

    <div class="health rc-panel">
      <div class="rc-row">
        <b class="rc-panel-title">后端组件</b>
        <span class="rc-dot" :class="`rc-dot--${ui.healthLight}`" />
        <span class="rc-mono rc-muted">{{ ui.healthLight }}</span>
        <span class="rc-spacer" />
        <button
          class="rc-btn rc-btn--ghost rc-btn--icon rc-btn--sm"
          type="button"
          title="重新检查"
          :disabled="ui.checkingHealth"
          @click="ui.refreshHealth()"
        >
          <PhArrowsClockwise :size="13" :class="{ spin: ui.checkingHealth }" />
        </button>
      </div>

      <div v-if="ui.health" class="comps">
        <div v-for="c in ui.health.components" :key="c.name" class="comp">
          <span class="rc-dot" :class="c.ok ? 'rc-dot--ok' : 'rc-dot--bad'" />
          <span class="rc-mono" style="width: 88px">{{ c.name }}</span>
          <span class="rc-muted rc-grow rc-truncate" :title="c.detail || ''">
            {{ c.detail || (c.ok ? 'ok' : '不可用') }}
          </span>
          <span v-if="c.latency_ms != null" class="rc-mono rc-muted">{{ c.latency_ms.toFixed(0) }} ms</span>
        </div>
      </div>
      <div v-else class="rc-caption" style="margin-top: 8px">
        {{ ui.healthError || '正在检查…' }}
      </div>
    </div>

    <ToolConsole />
  </div>
</template>

<style scoped>
.health {
  padding: 10px 12px;
  margin-bottom: 12px;
}
.comps {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.comp {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
}

.spin {
  animation: rc-rotate 0.7s linear infinite;
}
</style>
