<script setup lang="ts">
/**
 * 渲染一段 Markdown 正文，并处理引用角标与 LaTeX 公式。
 *
 * 为什么不直接在页面里 `v-html`：角标装饰与 MathJax 都需要拿到真实 DOM 节点，
 * 封装成组件后每处只关心 `:source`，DOM 操作被关在这一个文件里。
 */
const props = withDefaults(
  defineProps<{
    source: string
    /** 已通过 NLI 校验的引用编号；给了就会把未校验的角标标红 */
    verified?: Set<number> | number[]
    /** 流式输出中：末尾加闪烁光标 */
    streaming?: boolean
  }>(),
  { streaming: false },
)

const root = ref<HTMLElement | null>(null)
const html = computed(() => renderMarkdown(props.source || ''))

const verifiedSet = computed<Set<number> | undefined>(() => {
  if (!props.verified) return undefined
  return props.verified instanceof Set ? props.verified : new Set(props.verified)
})

function refresh() {
  const el = root.value
  if (!el || !import.meta.client) return
  decorateCitations(el, verifiedSet.value)
  typesetMath(el)
}

onMounted(refresh)
watch([html, verifiedSet], () => nextTick(refresh))
</script>

<template>
  <div
    ref="root"
    class="md-body"
    :class="{ 'md-caret': streaming }"
    v-html="html"
  />
</template>
