<script setup lang="ts">
/**
 * 输入框。
 *
 * 一个关键细节：`isComposing` 判断。中文/日文输入法在按回车选词时也会派发 keydown，
 * 不判这个标志的话，"输入'检索'然后选词"会把半截话直接发出去 —— 中文用户
 * 每次都会遇到，英文用户永远碰不到，所以这类 bug 特别容易漏测。
 */
import { PhFunnel, PhPaperPlaneRight, PhQuotes, PhStop, PhX } from '@phosphor-icons/vue'

const chat = useChatStore()
const library = useLibraryStore()

const text = ref('')
const area = ref<HTMLTextAreaElement | null>(null)

/** 自适应高度：2 行起步，最多约 10 行，再多就内部滚动 */
function autosize() {
  const el = area.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 200)}px`
}

function submit() {
  const body = text.value.trim()
  if (!body || chat.sending) return
  text.value = ''
  void nextTick(autosize)
  void chat.send(body, library.scopePaperIds)
}

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Enter' || e.shiftKey) return
  // 输入法组词中的回车属于"选字"，不是"发送"
  if (e.isComposing || e.keyCode === 229) return
  e.preventDefault()
  submit()
}

const scopeText = computed(() =>
  library.scopeNarrowed ? `${library.scopePaperIds.length} 篇` : '全库',
)

onMounted(autosize)
</script>

<template>
  <div class="shrink-0 border-t border-hairline bg-surface">
    <!-- 引文上下文：明着显示"你刚才选了这段"，而不是偷偷拼进问句 -->
    <div
      v-if="chat.quote"
      class="flex items-start gap-1.5 border-b border-hairline bg-brand-soft px-2.5 py-1.5"
    >
      <PhQuotes :size="11" class="mt-0.5 shrink-0 text-brand" />
      <p class="line-clamp-2 min-w-0 flex-1 text-2xs leading-relaxed text-brand-ink">
        <b>{{ chat.quote.paperTitle }}</b> · 第 {{ chat.quote.page }} 页：{{ chat.quote.text }}
      </p>
      <button
        type="button"
        class="shrink-0 text-brand-ink hover:opacity-70"
        title="取消引文上下文"
        @click="chat.clearQuote()"
      >
        <PhX :size="11" />
      </button>
    </div>

    <div class="px-2.5 pt-2">
      <textarea
        ref="area"
        v-model="text"
        class="max-h-50 w-full resize-none overflow-y-auto scroll-slim border-0 bg-transparent p-0 text-[13px] leading-relaxed text-ink outline-none placeholder:text-ink-4 focus:outline-none"
        rows="2"
        placeholder="问点什么…（Enter 发送 / Shift+Enter 换行）"
        aria-label="提问"
        @input="autosize"
        @keydown="onKeydown"
      />
    </div>

    <div class="flex items-center gap-1.5 px-2.5 py-2">
      <span :class="pillCls(scopeText === '全库' ? 'default' : 'brand')" :title="`检索范围：${scopeText}`">
        <PhFunnel :size="9" />
        {{ scopeText }}
      </span>

      <span class="min-w-0 flex-1" />

      <button
        v-if="chat.sending"
        type="button"
        :class="btnCls('default', { size: 'sm' })"
        @click="chat.abortStream()"
      >
        <PhStop :size="10" weight="fill" />
        停止
      </button>
      <button
        type="button"
        :class="btnCls('primary', { size: 'sm' })"
        :disabled="!text.trim() || chat.sending"
        @click="submit"
      >
        <PhPaperPlaneRight :size="11" />
        发送
      </button>
    </div>
  </div>
</template>
