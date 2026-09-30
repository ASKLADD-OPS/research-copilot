<script setup lang="ts">
/**
 * 极简外壳：只放页头标与居中容器，**没有导航**。
 *
 * 为什么登录/注册页要剥掉导航：这两个页面的唯一目标是完成表单。
 * 参考站也是这么做的（`/user/signup` 不带产品导航）。留一个返回首页的入口就够了。
 */
</script>

<template>
  <div class="flex min-h-screen flex-col bg-canvas">
    <header class="px-5 py-6 sm:px-8">
      <NuxtLink to="/" class="inline-block" aria-label="返回首页">
        <MarketingLogo />
      </NuxtLink>
    </header>

    <main class="flex flex-1 items-start justify-center px-5 pb-20 sm:px-8">
      <slot />
    </main>
  </div>
</template>
