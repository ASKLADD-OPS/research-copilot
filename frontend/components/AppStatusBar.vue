<script setup lang="ts">
/**
 * 底部状态条。
 *
 * 存在的理由：三栏各自只讲自己那段的事，"库里一共多少篇、现在检索范围是哪儿、
 * 后台还有没有在跑"这三件事没有归属，而它们每时每刻都要能瞄到。
 */
import { PhBookOpen, PhFunnel, PhSelectionAll } from '@phosphor-icons/vue'

const library = useLibraryStore()
const selection = useSelectionStore()
const chat = useChatStore()

const scopeText = computed(() =>
  library.scopeNarrowed ? `已选 ${library.scopePaperIds.length} 篇` : '全库',
)

const tokens = computed(() => {
  const u = chat.usage
  const total = u.total_tokens ?? (u.prompt_tokens ?? 0) + (u.completion_tokens ?? 0)
  return total || null
})
</script>

<template>
  <footer
    class="flex h-7 shrink-0 items-center gap-3 border-t border-hairline bg-surface px-3 text-2xs text-ink-3"
  >
    <span class="inline-flex items-center gap-1.5">
      <PhBookOpen :size="11" />
      共 {{ library.total }} 篇
      <span v-if="library.readyItems.length" class="text-ok">可检索 {{ library.readyItems.length }}</span>
      <span v-if="library.indexingItems.length" class="inline-flex items-center gap-1 text-brand">
        <AppSpinner :size="9" />{{ library.indexingItems.length }} 处理中
      </span>
      <span v-if="library.failedItems.length" class="text-bad">失败 {{ library.failedItems.length }}</span>
    </span>

    <span class="h-3 w-px bg-hairline" />

    <span class="inline-flex items-center gap-1.5">
      <PhFunnel :size="11" />
      检索范围 {{ scopeText }}
    </span>

    <span v-if="selection.selectionCount" class="inline-flex items-center gap-1.5">
      <PhSelectionAll :size="11" />
      划词 {{ selection.selectionCount }}
    </span>

    <span class="min-w-0 flex-1" />

    <span v-if="chat.sending" class="inline-flex items-center gap-1.5 text-brand">
      <AppSpinner :size="9" />
      生成中…
    </span>
    <span v-else-if="tokens">{{ tokens.toLocaleString() }} tok</span>

    <span v-if="selection.active" class="truncate">
      {{ selection.active.title }} · 第 {{ selection.page }} 页
    </span>
  </footer>
</template>
