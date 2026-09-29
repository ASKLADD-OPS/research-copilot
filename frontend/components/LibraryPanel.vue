<script setup lang="ts">
/**
 * 文献库面板：搜索 / 筛选 / 拖拽上传 / 列表 / 检索范围。
 *
 * 上传走"拖进面板就收"而不是弹一个上传对话框：拖拽是这个界面的主要输入方式，
 * 多一步确认只是多一步。多文件按顺序传，因为后端是流式落盘 + 立刻起解析任务，
 * 并发上传只会让进度条互相打架。
 */
import { PhBooks, PhFunnel, PhMagnifyingGlass, PhUploadSimple, PhX } from '@phosphor-icons/vue'
import type { Paper, PaperStatus } from '~/types/api'
import { STATUS_LABEL } from '~/stores/library'

defineProps<{ collapsed: boolean }>()
const emit = defineEmits<{ toggle: [] }>()

const library = useLibraryStore()

const STATUS_OPTIONS: ReadonlyArray<{ value: PaperStatus | ''; label: string }> = [
  { value: '', label: '全部状态' },
  { value: 'ready', label: STATUS_LABEL.ready },
  { value: 'parsing', label: STATUS_LABEL.parsing },
  { value: 'indexing', label: STATUS_LABEL.indexing },
  { value: 'failed', label: STATUS_LABEL.failed },
]

const fileInput = ref<HTMLInputElement | null>(null)
const dragDepth = ref(0)
const dragging = computed(() => dragDepth.value > 0)
const uploading = ref('')

// ---------------- 搜索防抖 ----------------
// 每敲一个字就打一次接口会让"共 N 篇"这个数字一路乱跳，也白烧后端连接
const searchText = ref(library.query)
let searchTimer: ReturnType<typeof setTimeout> | undefined

watch(searchText, (v) => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    library.query = v.trim()
    void library.load({ reset: true })
  }, 300)
})

watch(
  () => [library.statusFilter],
  () => void library.load({ reset: true }),
)

function onStatusInput(e: Event) {
  library.statusFilter = (e.target as HTMLSelectElement).value as PaperStatus | ''
}

// ---------------- 上传 ----------------
async function accept(files: FileList | File[] | null) {
  const list = Array.from(files ?? []).filter((f) => f.size > 0)
  if (!list.length) return
  for (const file of list) {
    uploading.value = file.name
    try {
      await library.upload(file)
    } catch (err) {
      alert(`《${file.name}》上传失败：${(err as Error).message}`)
    }
  }
  uploading.value = ''
}

function onDrop(e: DragEvent) {
  dragDepth.value = 0
  void accept(e.dataTransfer?.files ?? null)
}

function onPick(e: Event) {
  const input = e.target as HTMLInputElement
  void accept(input.files)
  input.value = '' // 允许连续两次选同一个文件
}

// ---------------- 编辑 ----------------
const editing = ref<Paper | null>(null)
const form = reactive({ title: '', abstract: '', year: '' as number | '', venue: '', tags: '' })
const saving = ref(false)

function openEdit(p: Paper) {
  editing.value = p
  form.title = p.title
  form.abstract = p.abstract ?? ''
  form.year = p.year ?? ''
  form.venue = p.venue ?? ''
  form.tags = (p.tags ?? []).join(', ')
}

async function saveEdit() {
  if (!editing.value) return
  saving.value = true
  try {
    await library.update(editing.value.id, {
      title: form.title.trim(),
      abstract: form.abstract.trim(),
      year: form.year === '' ? null : Number(form.year),
      venue: form.venue.trim(),
      tags: form.tags
        .split(/[,，]/)
        .map((t) => t.trim())
        .filter(Boolean),
    })
    editing.value = null
  } catch (err) {
    alert(`保存失败：${(err as Error).message}`)
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  void library.load({ reset: true }).then(() => library.resumePolling())
})
</script>

<template>
  <AppPanel
    title="文献库"
    :icon="PhBooks"
    side="left"
    :collapsed="collapsed"
    :meta="library.total ? `${library.total} 篇` : ''"
    @toggle="emit('toggle')"
  >
    <template #actions>
      <button
        type="button"
        class="grid size-6 place-items-center rounded-sm text-ink-3 transition-colors hover:bg-hover hover:text-ink"
        title="上传 PDF（也可以直接把文件拖进来）"
        @click="fileInput?.click()"
      >
        <PhUploadSimple :size="12" />
      </button>
    </template>

    <div
      class="relative flex h-full flex-col"
      @dragenter.prevent="dragDepth++"
      @dragover.prevent
      @dragleave="dragDepth = Math.max(0, dragDepth - 1)"
      @drop.prevent="onDrop"
    >
      <!-- 搜索 + 筛选 -->
      <div class="shrink-0 space-y-1.5 border-b border-hairline px-2.5 py-2">
        <div class="relative">
          <PhMagnifyingGlass
            :size="12"
            class="pointer-events-none absolute top-1/2 left-2 -translate-y-1/2 text-ink-4"
          />
          <input
            v-model="searchText"
            :class="INPUT_CLS"
            class="pl-6.5"
            type="search"
            placeholder="搜标题 / 作者 / 摘要"
            aria-label="搜索文献"
          />
        </div>
        <div class="flex items-center gap-1.5">
          <PhFunnel :size="11" class="shrink-0 text-ink-4" />
          <select
            :class="SELECT_CLS"
            :value="library.statusFilter"
            aria-label="按状态筛选"
            @change="onStatusInput"
          >
            <option v-for="o in STATUS_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
        </div>
      </div>

      <!-- 列表 -->
      <div class="min-h-0 flex-1 overflow-y-auto scroll-slim">
        <p v-if="uploading" class="m-2 rounded-md bg-brand-soft px-2 py-1.5 text-2xs text-brand-ink">
          正在上传 {{ uploading }}…
        </p>

        <!-- 错误必须排在骨架前面：loaded 只在成功时置 true，
             若骨架用 !loaded 且排在前面，请求一失败就会永远停在骨架屏上，
             errorMessage 永远没机会显示。 -->
        <div v-if="library.errorMessage" class="m-2.5 rounded-md bg-bad-soft px-2 py-1.5 text-2xs text-bad">
          {{ library.errorMessage }}
        </div>

        <div v-else-if="!library.loaded" class="space-y-2 p-2.5">
          <span v-for="i in 6" :key="i" class="block h-9 animate-pulse rounded-md bg-sunken" />
        </div>

        <div v-else-if="!library.items.length" :class="EMPTY_CLS">
          <PhBooks :size="18" class="mb-1 text-ink-4" />
          <strong class="text-[12.5px] text-ink-2">
            {{ library.query || library.statusFilter ? '没有匹配的文献' : '文献库还是空的' }}
          </strong>
          <span class="text-2xs text-ink-4">
            {{ library.query || library.statusFilter ? '换个关键词或清掉筛选' : '把 PDF 拖进这个面板就能开始' }}
          </span>
        </div>

        <template v-else>
          <LibraryItem
            v-for="(p, i) in library.items"
            :key="p.id"
            :paper="p"
            :index="i"
            @edit="openEdit"
          />
          <button
            v-if="library.hasMore"
            type="button"
            :class="btnCls('ghost', { size: 'sm', block: true })"
            class="rounded-none py-2"
            :disabled="library.loading"
            @click="library.page++; library.load()"
          >
            <AppSpinner v-if="library.loading" :size="11" />
            加载更多（还有 {{ library.total - library.items.length }} 篇）
          </button>
        </template>
      </div>

      <!-- 检索范围：常驻，因为它是"问什么"的前置条件 -->
      <div class="shrink-0 border-t border-hairline px-2.5 py-2">
        <div class="flex items-center gap-1.5">
          <span class="text-2xs text-ink-3">
            检索范围
            <b class="font-semibold text-ink-2">
              {{ library.scopeNarrowed ? `${library.scopePaperIds.length} 篇` : '全库' }}
            </b>
          </span>
          <span class="min-w-0 flex-1" />
          <button
            v-if="library.scopeNarrowed"
            type="button"
            :class="btnCls('ghost', { size: 'sm' })"
            @click="library.scopeToAll()"
          >
            <PhX :size="10" />
            清空
          </button>
          <button
            type="button"
            :class="btnCls('ghost', { size: 'sm' })"
            :disabled="!library.readyItems.length"
            title="把当前页「可检索」的文献全设为范围"
            @click="library.scopeToReady()"
          >
            仅选可检索
          </button>
        </div>
      </div>

      <!-- 拖拽遮罩 -->
      <div
        v-if="dragging"
        class="pointer-events-none absolute inset-0 z-20 grid place-items-center border-2 border-dashed border-brand bg-brand-soft"
      >
        <div class="text-center">
          <PhUploadSimple :size="22" class="mx-auto mb-1 text-brand" />
          <p class="text-[12.5px] font-medium text-brand-ink">松手即上传</p>
        </div>
      </div>
    </div>

    <input ref="fileInput" type="file" accept="application/pdf,.pdf" multiple class="hidden" @change="onPick" />

    <template #footer>
      <p class="px-2.5 py-1.5 text-2xs leading-relaxed text-ink-4">
        解析链：MinerU（版面 / 公式 / 表格）→ 失败降级 PyMuPDF。
        <b class="font-medium text-ink-3">状态变成「可检索」才会进入问答候选集。</b>
      </p>
    </template>
  </AppPanel>

  <Modal
    :model-value="editing !== null"
    title="编辑元数据"
    @update:model-value="editing = null"
  >
    <div class="space-y-2.5">
      <label :class="FIELD_CLS">
        <span :class="LABEL_CLS">标题</span>
        <input v-model="form.title" :class="INPUT_CLS" />
      </label>
      <div class="grid grid-cols-2 gap-2">
        <label :class="FIELD_CLS">
          <span :class="LABEL_CLS">年份</span>
          <input v-model="form.year" :class="INPUT_CLS" type="number" min="1900" max="2100" />
        </label>
        <label :class="FIELD_CLS">
          <span :class="LABEL_CLS">来源</span>
          <input v-model="form.venue" :class="INPUT_CLS" placeholder="会议 / 期刊" />
        </label>
      </div>
      <label :class="FIELD_CLS">
        <span :class="LABEL_CLS">标签（逗号分隔）</span>
        <input v-model="form.tags" :class="INPUT_CLS" placeholder="RAG, 检索增强" />
      </label>
      <label :class="FIELD_CLS">
        <span :class="LABEL_CLS">摘要</span>
        <textarea v-model="form.abstract" :class="TEXTAREA_CLS" rows="6" />
      </label>
    </div>
    <template #footer>
      <button type="button" :class="btnCls('ghost')" @click="editing = null">取消</button>
      <button type="button" :class="btnCls('primary')" :disabled="saving" @click="saveEdit">
        <AppSpinner v-if="saving" :size="11" />
        保存
      </button>
    </template>
  </Modal>
</template>
