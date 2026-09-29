<script setup lang="ts">
/** 写作页。 */
const papers = usePapersStore()
const ui = useUiStore()

onMounted(() => void papers.load())
</script>

<template>
  <div class="rc-page">
    <header class="rc-page-head">
      <h1>写作</h1>
      <p>
        默认走「检索 → 有据生成」：先从库里取证据，再让模型带着编号上下文写。
        生成后同样过溯源引擎，所以结果面板里一定有有据率与引用列表。
      </p>
    </header>

    <WritingStudio />
  </div>
</template>
