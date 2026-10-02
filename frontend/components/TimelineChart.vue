<script setup lang="ts">
/**
 * 方法演进时间轴（跨篇对比专用）。
 *
 * 年份**不是模型写的**，是后端从 arXiv 编号前缀推的（见 qa.py 的 `year_from_arxiv`）——
 * 没有年份的论文不编一个，直接显示 `—` 沉到末尾。这张图的全部价值在于"哪篇更早"
 * 是硬事实，一旦让模型复述年份，它就不再是证据了。
 *
 * 点击进那篇论文：时间轴只回答"先后"，具体方法差异还得回原文看。
 */
import { PhClockCounterClockwise } from '@phosphor-icons/vue'
import type { TimelineItem } from '~/types/api'

const props = defineProps<{ items: TimelineItem[] }>()

const selection = useSelectionStore()
const ui = useUiStore()

function open(it: TimelineItem) {
  if (it.paper_id == null) return
  ui.setMainView('reader')
  // 时间轴不知道命中的是哪一页，跳到首页即可 —— 用户此时是去通读，不是去核对某一句
  void selection.openAt(String(it.paper_id), 1)
}
</script>

<template>
  <section v-if="props.items.length" class="rounded-md border border-hairline bg-sunken p-2">
    <p class="mb-1.5 flex items-center gap-1.5 text-2xs font-medium text-ink-3">
      <PhClockCounterClockwise :size="11" />
      方法演进
      <span class="font-normal text-ink-4">{{ props.items.length }} 篇 · 按 arXiv 年份升序</span>
    </p>

    <ol>
      <li v-for="(it, i) in props.items" :key="it.paper_id ?? it.title" class="grid grid-cols-[38px_1fr] gap-2">
        <span
          class="pt-0.5 text-right font-mono text-[11px] tabular-nums"
          :class="it.year ? 'text-ink-2' : 'text-ink-4'"
        >
          {{ it.year ?? '—' }}
        </span>

        <!-- 左侧竖线 + 节点；最后一个条目不再留下方间距，免得时间轴拖出一条空尾巴 -->
        <div
          class="relative border-l border-hairline-2 pl-3"
          :class="i < props.items.length - 1 ? 'pb-2.5' : ''"
        >
          <span
            class="absolute top-1.5 -left-[2.5px] size-[5px] rounded-full"
            :class="it.year ? 'bg-brand' : 'bg-ink-4'"
          />

          <button
            type="button"
            class="block max-w-full truncate rounded-xs px-1 -mx-1 text-left text-[11.5px] font-medium transition-colors"
            :class="
              it.paper_id != null ? 'text-ink hover:bg-hover cursor-pointer' : 'text-ink-2 cursor-default'
            "
            :title="it.title"
            @click="open(it)"
          >
            {{ it.title || `论文 #${it.paper_id}` }}
          </button>

          <p class="mt-0.5 flex flex-wrap items-center gap-1.5 text-2xs text-ink-4">
            <span v-if="it.arxiv_id" class="font-mono">arXiv:{{ it.arxiv_id }}</span>
            <span>命中 {{ it.chunks }} 块</span>
          </p>
        </div>
      </li>
    </ol>
  </section>
</template>
