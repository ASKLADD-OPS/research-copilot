<script setup lang="ts">
/**
 * 数据可视化：拖 CSV 进来 → Agent 选图型 → 出 PNG + 学术图注。
 *
 * 为什么独立路由：这条链路是"上传一个文件、反复改图型、比着看"的循环，
 * 跟写作台的"大纲—正文—引用"没有共享状态，塞进右栏那 340px 只会两边都不好用。
 *
 * 数据流就两步，刻意不合并：
 *   POST /visualize/upload    → 落盘 + 判列类型，返回列画像（不调模型，即时返回）
 *   POST /visualize/generate  → 选图型 + 渲染 + 写图注（调模型，会花钱）
 * 分开是因为上传会被人反复点（换文件、看列名），而生成要点一次按钮。
 *
 * **模型只决定"画哪种图、用哪两列"**，数值一律由后端从 CSV 现取（见 app/visualize/csv_charts.py）。
 * 所以这里不需要展示或编辑任何生成的代码 —— 没有代码可展示。
 */
import {
  PhArrowLeft,
  PhChartLine,
  PhCopy,
  PhDownloadSimple,
  PhFileCsv,
  PhSparkle,
  PhUploadSimple,
  PhWarningCircle,
} from '@phosphor-icons/vue'
import type { ChartType, CsvDataset, VisualizeResult } from '~/types/api'

definePageMeta({ layout: false })

useHead({
  title: '数据可视化 · Research Copilot',
  meta: [{ name: 'robots', content: 'noindex, nofollow' }],
})

const api = useApi()

// ------------------------------------------------------------------ 上传
const dragDepth = ref(0)
const fileInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
const dataset = ref<CsvDataset | null>(null)
const uploadError = ref('')

/** 拖拽进入/离开会在子元素上反复触发，用计数器而不是布尔值，否则光标一动高亮就闪。 */
function onDrop(e: DragEvent) {
  dragDepth.value = 0
  void accept(e.dataTransfer?.files ?? null)
}

function onPick(e: Event) {
  const input = e.target as HTMLInputElement
  void accept(input.files)
  input.value = '' // 允许连续两次选同一个文件
}

async function accept(files: FileList | null) {
  const file = files?.[0]
  if (!file) return
  if (!/\.(csv|tsv|txt)$/i.test(file.name)) {
    uploadError.value = `只收 CSV / TSV / TXT，收到的是 ${file.name}`
    return
  }
  uploading.value = true
  uploadError.value = ''
  dataset.value = null
  result.value = null
  try {
    const form = new FormData()
    form.append('file', file)
    dataset.value = await api.upload<CsvDataset>('/visualize/upload', form)
    // 换了数据集，上一份的选图建议就不再适用，回到 auto
    chartType.value = 'auto'
  } catch (err) {
    uploadError.value = (err as Error).message
  } finally {
    uploading.value = false
  }
}

const DTYPE_LABEL: Record<string, string> = { numeric: '数值', datetime: '日期', text: '文本' }
const DTYPE_TONE: Record<string, 'brand' | 'ok' | 'default'> = {
  numeric: 'brand',
  datetime: 'ok',
  text: 'default',
}

function fmt(n: number | null | undefined): string {
  if (n == null) return '—'
  return Math.abs(n) >= 1000 || (n !== 0 && Math.abs(n) < 0.01) ? n.toExponential(2) : String(Math.round(n * 1000) / 1000)
}

/** 制表符直接印出来是个看不见的空白，写成 `Tab` 才读得懂。 */
const delimiterLabel = computed(() => {
  const d = dataset.value?.delimiter ?? ','
  if (d === '\t') return 'Tab'
  if (d === ' ') return '空格'
  return d
})

// ------------------------------------------------------------------ 生成
const CHART_TYPES: Array<{ value: ChartType | 'auto'; label: string; hint: string }> = [
  { value: 'auto', label: '自动（Agent 选）', hint: 'Agent 读列画像后决定画哪种，理由会写在结果里' },
  { value: 'line', label: '折线图', hint: '看趋势：x 是时间或有序量，y 是数值' },
  { value: 'bar', label: '柱状图', hint: '比大小：x 是分类，y 是数值；同一 x 多行取均值' },
  { value: 'scatter', label: '散点图', hint: '看相关性：两个数值列的关系' },
  { value: 'heatmap', label: '热力图', hint: '看矩阵：分组列 × 数值列的均值矩阵' },
  { value: 'boxplot', label: '箱线图', hint: '看分布：每个分组下数值列的四分位与离群点' },
  { value: 'radar', label: '雷达图', hint: '看多指标轮廓：至少三个数值列才画得出来' },
]

const chartType = ref<ChartType | 'auto'>('auto')
const instruction = ref('')
const generating = ref(false)
const generateError = ref('')
const result = ref<VisualizeResult | null>(null)

const chartHint = computed(
  () => CHART_TYPES.find((c) => c.value === chartType.value)?.hint ?? '',
)
const imageSrc = computed(() => (result.value?.image ? `data:image/png;base64,${result.value.image}` : ''))

async function generate() {
  if (!dataset.value || generating.value) return
  generating.value = true
  generateError.value = ''
  result.value = null
  try {
    result.value = await api.post<VisualizeResult>('/visualize/generate', {
      dataset_id: dataset.value.dataset_id,
      chart_type: chartType.value,
      instruction: instruction.value,
      title: '',
    })
  } catch (err) {
    generateError.value = (err as Error).message
  } finally {
    generating.value = false
  }
}

// ------------------------------------------------------------------ 导出
function downloadPng() {
  const b64 = result.value?.image
  if (!b64) return
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  const url = URL.createObjectURL(new Blob([bytes], { type: result.value?.mime || 'image/png' }))
  const a = document.createElement('a')
  a.href = url
  a.download = `${dataset.value?.filename.replace(/\.[^.]+$/, '') || 'chart'}-${result.value?.chart_type}.png`
  a.click()
  URL.revokeObjectURL(url)
}

/** 图注要跟着图一起进论文，所以单独给一个"连图注一起复制"的入口。 */
async function copyCaption() {
  if (!result.value?.caption) return
  try {
    await navigator.clipboard.writeText(result.value.caption)
    copied.value = true
    setTimeout(() => (copied.value = false), 1600)
  } catch {
    generateError.value = '浏览器拒绝了剪贴板访问，请手动选中图注复制。'
  }
}

const copied = ref(false)
</script>

<template>
  <div class="flex h-screen flex-col bg-canvas text-[12.5px] text-ink">
    <!-- ---------------------------------------------------------------- 顶栏 -->
    <header class="flex h-11 shrink-0 items-center gap-2 border-b border-hairline bg-surface px-3">
      <NuxtLink
        to="/app"
        class="inline-flex h-6.5 shrink-0 items-center gap-1 rounded-md px-2 text-[11.5px] text-ink-3 transition-colors hover:bg-hover hover:text-ink"
      >
        <PhArrowLeft :size="11" />
        工作台
      </NuxtLink>
      <span class="mx-1 h-4 w-px shrink-0 bg-hairline" />
      <span class="grid size-5 shrink-0 place-items-center rounded-[6px] bg-brand text-white">
        <PhChartLine :size="12" weight="fill" />
      </span>
      <span class="text-[13px] font-semibold tracking-tight">数据可视化</span>

      <span v-if="dataset" :class="pillCls('default')">{{ dataset.rows }} 行 × {{ dataset.columns.length }} 列</span>
      <span v-if="dataset?.truncated" :class="pillCls('warn')">已截断（原文件 {{ dataset.total_rows }} 行）</span>
      <span v-if="result" :class="pillCls(result.image ? 'ok' : 'warn')">
        {{ result.image ? `已出图 · ${result.renderer}` : `仅规格 · ${result.renderer}` }}
      </span>
      <span v-if="result" :class="pillCls('default')">{{ result.elapsed_ms }} ms</span>

      <span class="min-w-0 flex-1" />

      <button
        v-if="result?.image"
        type="button"
        :class="btnCls('ghost', { size: 'sm' })"
        title="复制图注（连图一起贴进论文时用）"
        @click="copyCaption"
      >
        <PhCopy :size="11" />
        {{ copied ? '已复制' : '图注' }}
      </button>
      <button
        v-if="result?.image"
        type="button"
        :class="btnCls('ghost', { size: 'sm' })"
        title="下载 PNG"
        @click="downloadPng"
      >
        <PhDownloadSimple :size="11" />
        PNG
      </button>
    </header>

    <div class="grid min-h-0 flex-1 grid-cols-[minmax(300px,360px)_1fr]">
      <!-- ------------------------------------------------------------ 左：数据 -->
      <aside class="flex min-h-0 flex-col border-r border-hairline bg-surface">
        <div class="shrink-0 space-y-2.5 border-b border-hairline px-3 py-3">
          <div
            class="cursor-pointer rounded-lg border border-dashed border-hairline-2 px-3 py-4 text-center transition-colors"
            :class="dragDepth > 0 ? 'border-brand bg-brand-soft' : 'hover:bg-hover'"
            @click="fileInput?.click()"
            @dragenter.prevent="dragDepth++"
            @dragover.prevent
            @dragleave="dragDepth = Math.max(0, dragDepth - 1)"
            @drop.prevent="onDrop"
          >
            <AppSpinner v-if="uploading" :size="16" class="mx-auto text-brand" />
            <PhUploadSimple v-else :size="16" class="mx-auto text-ink-4" />
            <p class="mt-1.5 text-[12.5px] font-medium text-ink-2">
              {{ uploading ? '正在解析…' : '拖 CSV 进来，或点击选择' }}
            </p>
            <p class="mt-0.5 text-2xs leading-relaxed text-ink-4">
              逗号 / 分号 / 制表符 / 竖线都能自动识别。文件只落在后端本地，不上传第三方。
            </p>
          </div>
          <input
            ref="fileInput"
            type="file"
            accept=".csv,.tsv,.txt,text/csv"
            class="hidden"
            @change="onPick"
          />
          <p v-if="uploadError" class="rounded-md bg-bad-soft px-2 py-1.5 text-2xs text-bad">{{ uploadError }}</p>
        </div>

        <!-- 列画像 -->
        <div class="min-h-0 flex-1 overflow-y-auto scroll-slim px-3 py-2.5">
          <div v-if="!dataset" :class="EMPTY_CLS">
            <strong class="text-[12.5px] text-ink-2">还没有数据</strong>
            <span class="text-2xs leading-relaxed text-ink-4">
              传一份 CSV，这里会列出每一列的类型、缺失和取值域 —— Agent 就是靠这些信息选图型的。
            </span>
          </div>

          <template v-else>
            <p class="mb-1.5 flex items-center gap-1.5">
              <PhFileCsv :size="12" class="shrink-0 text-ink-4" />
              <span class="min-w-0 flex-1 truncate font-medium text-ink-2" :title="dataset.filename">
                {{ dataset.filename }}
              </span>
            </p>
            <p class="mb-2 text-2xs text-ink-4">
              定界符 <code class="font-mono">{{ delimiterLabel }}</code>
              · 数值列 {{ dataset.numeric_columns.length }} · 分类列 {{ dataset.categorical_columns.length }}
            </p>

            <p v-if="dataset.warning" class="mb-2 rounded-md bg-warn-soft px-2 py-1.5 text-2xs leading-relaxed text-warn">
              <PhWarningCircle :size="11" class="mr-1 inline align-[-1px]" />
              {{ dataset.warning }}
            </p>

            <ul class="space-y-1">
              <li
                v-for="c in dataset.columns"
                :key="c.name"
                class="rounded-md border border-hairline px-2 py-1.5"
              >
                <span class="flex items-center gap-1.5">
                  <span class="min-w-0 flex-1 truncate font-medium text-ink" :title="c.name">{{ c.name }}</span>
                  <span :class="pillCls(DTYPE_TONE[c.dtype])">{{ DTYPE_LABEL[c.dtype] }}</span>
                  <span v-if="c.missing" :class="pillCls('warn')">缺 {{ c.missing }}</span>
                </span>
                <span class="mt-0.5 block text-2xs tabular-nums text-ink-4">
                  {{ c.unique }} 个不同值
                  <template v-if="c.dtype === 'numeric'"> · {{ fmt(c.min) }} ~ {{ fmt(c.max) }} · 均值 {{ fmt(c.mean) }}</template>
                </span>
                <span v-if="c.samples.length" class="mt-0.5 block truncate text-2xs text-ink-4" :title="c.samples.join(' / ')">
                  例：{{ c.samples.join(' / ') }}
                </span>
              </li>
            </ul>

            <details v-if="dataset.head.length" class="mt-3">
              <summary class="cursor-pointer text-2xs font-medium text-ink-3 hover:text-ink">前 {{ dataset.head.length }} 行原样</summary>
              <div class="mt-1.5 overflow-x-auto scroll-slim rounded-md border border-hairline">
                <table class="w-full text-2xs">
                  <thead class="bg-sunken">
                    <tr>
                      <th
                        v-for="c in dataset.columns"
                        :key="c.name"
                        class="px-1.5 py-1 text-left font-medium whitespace-nowrap text-ink-3"
                      >
                        {{ c.name }}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(row, i) in dataset.head" :key="i" class="border-t border-hairline">
                      <td v-for="(cell, j) in row" :key="j" class="px-1.5 py-1 whitespace-nowrap text-ink-2">
                        {{ cell || '—' }}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </details>
          </template>
        </div>
      </aside>

      <!-- ------------------------------------------------------------ 右：画什么 -->
      <main class="flex min-h-0 flex-col">
        <div class="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-hairline px-3 py-2">
          <select v-model="chartType" :class="SELECT_CLS" class="max-w-[180px]" aria-label="图表类型">
            <option v-for="c in CHART_TYPES" :key="c.value" :value="c.value">{{ c.label }}</option>
          </select>
          <input
            v-model="instruction"
            :class="INPUT_CLS"
            class="max-w-[300px] flex-1"
            placeholder="额外要求（可选），如：只看 2024 年之后"
            @keydown.enter="generate"
          />
          <button
            type="button"
            :class="btnCls('primary', { size: 'sm' })"
            :disabled="generating || !dataset"
            @click="generate"
          >
            <AppSpinner v-if="generating" :size="11" />
            <PhSparkle v-else :size="11" weight="fill" />
            生成图表
          </button>
        </div>

        <p class="shrink-0 border-b border-hairline px-3 py-1.5 text-2xs text-ink-4">{{ chartHint }}</p>

        <p v-if="generateError" class="shrink-0 bg-bad-soft px-3 py-1.5 text-2xs text-bad">{{ generateError }}</p>
        <p
          v-else-if="result?.warning"
          class="shrink-0 bg-warn-soft px-3 py-1.5 text-2xs leading-relaxed text-warn"
        >
          <PhWarningCircle :size="11" class="mr-1 inline align-[-1px]" />
          {{ result.warning }}
        </p>

        <div class="min-h-0 flex-1 overflow-y-auto scroll-slim p-3">
          <div v-if="generating" :class="EMPTY_CLS">
            <AppSpinner :size="18" class="text-brand" />
            <strong class="mt-1 text-[12.5px] text-ink-2">正在选图型并渲染…</strong>
            <span class="text-2xs leading-relaxed text-ink-4">一次模型调用 + 一次本地渲染，通常几秒。</span>
          </div>

          <template v-else-if="result">
            <figure class="mx-auto max-w-[860px]">
              <img
                v-if="imageSrc"
                :src="imageSrc"
                :alt="result.caption || '生成的图表'"
                class="w-full rounded-md border border-hairline bg-white"
              />
              <div v-else class="rounded-md border border-hairline bg-sunken px-3 py-4 text-center">
                <p class="text-[12.5px] font-medium text-ink-2">这次没渲染出图</p>
                <p class="mt-1 text-2xs leading-relaxed text-ink-4">
                  上方提示里写了原因（缺字体、列里没数、渲染器报错）。规格仍然是有效的，见下方。
                </p>
              </div>

              <!-- 图注：学术论文里图注在图**下方**，所以这里也放下面 -->
              <figcaption v-if="result.caption" class="mt-2 text-2xs leading-relaxed text-ink-2">
                {{ result.caption }}
              </figcaption>
            </figure>

            <section class="mx-auto mt-4 max-w-[860px] rounded-md border border-hairline bg-surface px-3 py-2.5">
              <p class="mb-1.5 flex flex-wrap items-center gap-1.5">
                <span class="text-2xs font-semibold text-ink-2">Agent 的选图结论</span>
                <span :class="pillCls('brand')">{{ result.chart_type }}</span>
                <span v-if="result.plan.x" :class="pillCls('default')">x = {{ result.plan.x }}</span>
                <span v-for="y in result.plan.y ?? []" :key="y" :class="pillCls('default')">y = {{ y }}</span>
              </p>
              <p class="text-2xs leading-relaxed text-ink-3">
                {{ result.rationale || result.plan.rationale || '（模型没给理由）' }}
              </p>
              <details class="mt-2">
                <summary class="cursor-pointer text-2xs font-medium text-ink-3 hover:text-ink">
                  真正喂给 matplotlib 的规格
                </summary>
                <pre class="mt-1.5 max-h-56 overflow-auto scroll-slim rounded-md bg-sunken px-2 py-1.5 font-mono text-[11px] leading-relaxed text-ink-2">{{ JSON.stringify(result.spec, null, 2) }}</pre>
              </details>
            </section>
          </template>

          <div v-else :class="EMPTY_CLS">
            <strong class="text-[12.5px] text-ink-2">{{ dataset ? '还没画' : '先传一份 CSV' }}</strong>
            <span class="text-2xs leading-relaxed text-ink-4">
              {{ dataset
                ? '左边是这份数据的列画像。选「自动」让 Agent 决定图型，或指定一种再点「生成图表」。'
                : '传完文件后，这里会出现 Agent 选的图型、渲染结果和学术风格的图注。' }}
            </span>
          </div>
        </div>
      </main>
    </div>
  </div>
</template>
