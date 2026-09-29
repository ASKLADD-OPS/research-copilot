<script setup lang="ts">
/** 一条引用。`verified=false` 说明 NLI 没验过 —— 视觉上必须一眼可辨。 */
import { PhArrowSquareOut } from '@phosphor-icons/vue'
import type { Citation } from '~/types/api'

const props = defineProps<{ citation: Citation; index?: number }>()
const emit = defineEmits<{ open: [citation: Citation] }>()

const title = computed(() => props.citation.title || '（未命名文献）')

const pages = computed(() => {
  const { page_start: s, page_end: e } = props.citation
  if (s && e && e !== s) return `p.${s}-${e}`
  return s ? `p.${s}` : ''
})

const score = computed(() => {
  const v = props.citation.nli_score
  return typeof v === 'number' ? `${(v * 100).toFixed(0)}%` : ''
})
</script>

<template>
  <button class="cite" type="button" @click="emit('open', citation)">
    <span class="cite-head">
      <span class="rc-cite-mark" aria-hidden="true">{{ citation.marker ?? index ?? '?' }}</span>
      <span class="rc-truncate rc-grow" :title="title">{{ title }}</span>
      <span class="rc-pill" :class="citation.verified ? 'rc-pill--ok' : 'rc-pill--bad'">
        {{ citation.verified ? `蕴含 ${score}` : '未验证' }}
      </span>
      <PhArrowSquareOut class="cite-out" :size="13" />
    </span>

    <span v-if="citation.section || pages" class="rc-mono cite-meta">
      <span v-if="citation.section">{{ citation.section }}</span>
      <span v-if="pages">{{ pages }}</span>
    </span>

    <span v-if="citation.quote" class="cite-quote">{{ citation.quote }}</span>
  </button>
</template>

<style scoped>
.cite {
  display: block;
  width: 100%;
  padding: 8px 10px;
  text-align: left;
  background: var(--rc-surface-1);
  border: 1px solid var(--rc-hairline);
  border-radius: var(--rc-radius-md);
  color: inherit;
  cursor: pointer;
  transition: border-color 0.14s ease;
}
.cite:hover {
  border-color: var(--rc-hairline-strong);
  background: var(--rc-surface-2);
}

.cite-head {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12.5px;
}
.cite-out {
  color: var(--rc-ink-tertiary);
  flex: 0 0 auto;
}

.cite-meta {
  display: flex;
  gap: 10px;
  margin-top: 4px;
  color: var(--rc-ink-tertiary);
}

.cite-quote {
  display: -webkit-box;
  -webkit-line-clamp: 3;
  line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  margin-top: 6px;
  padding-left: 8px;
  border-left: 2px solid var(--rc-hairline-strong);
  color: var(--rc-ink-subtle);
  font-size: 12px;
  line-height: 1.6;
}
</style>
