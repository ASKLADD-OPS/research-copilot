<script setup lang="ts">
/**
 * 输入区：正文 + 检索范围 + 发送/停止。
 *
 * 检索范围用原生 `<details>` 做浮层：它自带开合与键盘行为，不需要为一个小选择器
 * 引 popover 库或手写全局点击监听。
 */
import { PhPaperPlaneRight, PhStop, PhFunnel } from '@phosphor-icons/vue'
import type { Paper } from '~/types/api'

const props = withDefaults(
  defineProps<{
    sending?: boolean
    papers?: Paper[]
    placeholder?: string
  }>(),
  {
    sending: false,
    papers: () => [],
    placeholder: '问点什么…… Enter 发送，Shift+Enter 换行',
  },
)

const emit = defineEmits<{ send: [query: string, paperIds: string[]]; stop: [] }>()

const text = ref('')
const scope = ref<string[]>([])
const box = ref<HTMLTextAreaElement | null>(null)

const readyPapers = computed(() => props.papers.filter((p) => p.status === 'ready'))

const scopeLabel = computed(() => (scope.value.length ? `限定 ${scope.value.length} 篇` : '全库检索'))

function toggle(id: string) {
  const i = scope.value.indexOf(id)
  if (i >= 0) scope.value.splice(i, 1)
  else scope.value.push(id)
}

function clearScope() {
  scope.value = []
}

function submit() {
  const q = text.value.trim()
  if (!q || props.sending) return
  emit('send', q, [...scope.value])
  text.value = ''
  if (box.value) box.value.style.height = 'auto'
}

/* Enter 发送，Shift+Enter 换行 —— 与主流对话产品一致。
 * isComposing 必须判：中文输入法选词时按 Enter 不该发出去。 */
function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Enter' || e.shiftKey) return
  if (e.isComposing || (e as KeyboardEvent & { keyCode?: number }).keyCode === 229) return
  e.preventDefault()
  submit()
}

/** 自动长高到 8 行封顶，再长就滚动。 */
function autogrow() {
  const el = box.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 176)}px`
}
</script>

<template>
  <div class="composer">
    <textarea
      ref="box"
      v-model="text"
      class="rc-textarea composer-input"
      rows="1"
      :placeholder="placeholder"
      :disabled="sending"
      aria-label="输入问题"
      @input="autogrow"
      @keydown="onKeydown"
    />

    <div class="composer-bar">
      <details class="scope">
        <summary class="rc-pill" :class="scope.length ? 'rc-pill--primary' : 'rc-pill--dim'">
          <PhFunnel :size="11" />
          {{ scopeLabel }}
        </summary>

        <div class="scope-pop rc-panel">
          <div class="rc-row scope-head">
            <span class="rc-caption rc-grow">只在这几篇里检索（不选 = 全库）</span>
            <button v-if="scope.length" class="rc-btn rc-btn--bare rc-btn--sm" type="button" @click="clearScope">
              清空
            </button>
          </div>

          <div class="rc-scroll scope-list">
            <label v-for="p in readyPapers" :key="p.id" class="scope-item">
              <input type="checkbox" :checked="scope.includes(p.id)" @change="toggle(p.id)" />
              <span class="rc-truncate" :title="p.title">{{ p.title || '（未命名）' }}</span>
            </label>

            <p v-if="!readyPapers.length" class="rc-caption" style="padding: 8px">
              文献库里还没有「可检索」的论文，先去上传解析。
            </p>
          </div>
        </div>
      </details>

      <span class="rc-spacer" />

      <button v-if="sending" class="rc-btn rc-btn--secondary" type="button" @click="emit('stop')">
        <PhStop :size="13" weight="fill" />
        停止
      </button>
      <button v-else class="rc-btn rc-btn--primary" type="button" :disabled="!text.trim()" @click="submit">
        <PhPaperPlaneRight :size="13" weight="fill" />
        发送
      </button>
    </div>
  </div>
</template>

<style scoped>
.composer {
  border: 1px solid var(--rc-hairline);
  border-radius: var(--rc-radius-lg);
  background: var(--rc-surface-1);
  transition: border-color 0.14s ease;
}
.composer:focus-within {
  border-color: var(--rc-hairline-strong);
}

.composer-input {
  border: none;
  background: transparent;
  border-radius: var(--rc-radius-lg);
  padding: 11px 13px 4px;
  font-size: 13.5px;
  max-height: 176px;
}
.composer-input:hover,
.composer-input:focus {
  border: none;
  box-shadow: none;
}

.composer-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px 7px 11px;
}

.scope {
  position: relative;
}
.scope > summary {
  list-style: none;
  cursor: pointer;
}
.scope > summary::-webkit-details-marker {
  display: none;
}

.scope-pop {
  position: absolute;
  z-index: 20;
  bottom: calc(100% + 7px);
  left: 0;
  width: 340px;
  box-shadow: var(--rc-shadow-pop);
  overflow: hidden;
}
.scope-head {
  padding: 7px 9px;
  border-bottom: 1px solid var(--rc-hairline);
}
.scope-list {
  max-height: 240px;
  padding: 4px;
}
.scope-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 7px;
  border-radius: var(--rc-radius-sm);
  font-size: 12.5px;
  cursor: pointer;
}
.scope-item:hover {
  background: var(--rc-surface-2);
}
.scope-item input {
  accent-color: var(--rc-primary);
  flex: 0 0 auto;
}
</style>
