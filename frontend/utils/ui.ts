import type { PaperStatus } from '~/types/api'
import type { NormRect } from '~/types/workbench'

/**
 * 共享的类名常量。
 *
 * 为什么不是组件：按钮/输入框/胶囊在这个界面里出现上百次，做成组件就会带来
 * 一堆"只用两三次"的 props（size / tone / loading / icon / block…），
 * 而它们的结果本来就是一行类名。集中在这里而不是散在各组件里，是因为
 * "主按钮长什么样"改一次就该全站生效。
 *
 * 类名必须写成完整字面量 —— Tailwind 是扫源码文本收集候选的，
 * 拼出来的 `bg-${tone}-500` 它看不到，会在构建时被摇掉。
 */

export type BtnTone = 'primary' | 'default' | 'ghost' | 'danger'
export type PillTone = 'default' | 'brand' | 'ok' | 'warn' | 'bad'

export function btnCls(
  tone: BtnTone = 'default',
  opts: { size?: 'sm' | 'md'; block?: boolean } = {},
): string {
  const { size = 'md', block = false } = opts
  const base =
    'inline-flex shrink-0 items-center justify-center gap-1.5 rounded-md font-medium whitespace-nowrap transition-colors disabled:pointer-events-none disabled:opacity-40'
  const sizing = size === 'sm' ? 'h-6.5 px-2 text-[11.5px]' : 'h-8 px-3 text-[12.5px]'
  const toneCls = {
    // 主按钮用墨色实底，跟 ponder 的 CTA 一致：整个界面里不出现第二种饱和色
    primary: 'bg-ink text-white hover:bg-ink/88 active:bg-ink/80',
    default: 'border border-hairline-2 bg-surface text-ink hover:bg-hover active:bg-active',
    ghost: 'text-ink-2 hover:bg-hover hover:text-ink',
    danger: 'border border-hairline-2 bg-surface text-bad hover:bg-bad-soft',
  }[tone]
  return [base, sizing, toneCls, block ? 'w-full' : ''].filter(Boolean).join(' ')
}

/** 只有图标的方形按钮。热区与普通按钮同高，视觉上不占宽度。 */
export function iconBtnCls(size: 'sm' | 'md' = 'md', tone: BtnTone = 'ghost'): string {
  const box = size === 'sm' ? 'size-6' : 'size-8'
  return `${btnCls(tone, { size })} ${box} px-0`
}

export function pillCls(tone: PillTone = 'default'): string {
  const base =
    'inline-flex shrink-0 items-center gap-1 rounded-full px-1.5 py-0.5 text-2xs font-medium whitespace-nowrap'
  const toneCls = {
    default: 'bg-sunken text-ink-3',
    brand: 'bg-brand-soft text-brand-ink',
    ok: 'bg-ok-soft text-ok',
    warn: 'bg-warn-soft text-warn',
    bad: 'bg-bad-soft text-bad',
  }[tone]
  return `${base} ${toneCls}`
}

export const INPUT_CLS =
  'h-7 w-full min-w-0 rounded-md border border-hairline-2 bg-surface px-2 text-[12.5px] text-ink placeholder:text-ink-4 transition-colors focus:border-brand focus:outline-none'

export const SELECT_CLS =
  'h-7 w-full cursor-pointer rounded-md border border-hairline-2 bg-surface px-1.5 text-[12.5px] text-ink focus:border-brand focus:outline-none'

export const TEXTAREA_CLS =
  'w-full resize-none rounded-md border border-hairline-2 bg-surface px-2 py-1.5 text-[12.5px] leading-relaxed text-ink placeholder:text-ink-4 focus:border-brand focus:outline-none'

export const LABEL_CLS = 'block text-2xs font-medium text-ink-3'

/** 表单字段的竖排容器：标签在上，控件在下，间距统一。 */
export const FIELD_CLS = 'flex flex-col gap-1'

export const MONO_CLS = 'font-mono text-[11.5px] tabular-nums'

/** 面板里的行内工具条。 */
export const TOOLBAR_CLS = 'flex h-9 shrink-0 items-center gap-1.5 border-b border-hairline px-2'

/** 空状态：标题 + 说明两行，居中。 */
export const EMPTY_CLS = 'flex flex-col items-center gap-1 px-4 py-8 text-center'

export function statusTone(status: PaperStatus): PillTone {
  switch (status) {
    case 'ready':
      return 'ok'
    case 'failed':
      return 'bad'
    case 'parsing':
    case 'indexing':
      return 'brand'
    default:
      return 'default'
  }
}

/** 有据率 → 语气。0.8 是后端的验收阈值（GROUNDING_THRESHOLD），前端跟着走。 */
export function groundingTone(ratio: number): PillTone {
  if (ratio >= 0.8) return 'ok'
  if (ratio >= 0.6) return 'warn'
  return 'bad'
}

/**
 * 后端 bbox → 归一化矩形。
 *
 * `Chunk.bbox` 有两种形态（见 backend/app/models/chunk.py）：单区域 `[x0,y0,x1,y1]`、
 * 多区域 `{page, boxes: [[x0,y0,x1,y1], ...]}`。两者都是**归一化**坐标，
 * 所以这里只做「角点 → 宽高」的换算，不做任何缩放。
 *
 * 放在 utils 而不是组件里：阅读器（按坐标画框）与溯源列表（把 bbox 喂给跳转指令）
 * 都要用它，留在组件里就得复制一份。
 */
export function rectsFromBbox(bbox: unknown): NormRect[] {
  const corners = (b: unknown): NormRect | null => {
    if (!Array.isArray(b) || b.length < 4) return null
    const [x0, y0, x1, y1] = b.slice(0, 4).map(Number)
    if (![x0, y0, x1, y1].every(Number.isFinite)) return null
    return { x: Math.min(x0, x1), y: Math.min(y0, y1), w: Math.abs(x1 - x0), h: Math.abs(y1 - y0) }
  }
  const direct = corners(bbox)
  if (direct) return [direct]
  const boxes = (bbox as { boxes?: unknown[] } | null | undefined)?.boxes
  return Array.isArray(boxes) ? boxes.map(corners).filter((r): r is NormRect => r !== null) : []
}

// ---------------------------------------------------------------- 段落 ↔ PDF 位置

/** 一条"在 PDF 上有位置"的段落。`bbox` 形态同 `Chunk.bbox`。 */
export interface AnchoredSegment {
  id: string
  /** 1-based */
  page?: number | null
  bbox?: unknown
}

/** 段落中心在页面上的归一化纵坐标；没有 bbox 就是 null（定位不了，只能跳过）。 */
export function centerY(bbox: unknown): number | null {
  const r = rectsFromBbox(bbox)[0]
  return r ? r.y + r.h / 2 : null
}

/**
 * 同页里 bbox 中心最接近 `y` 的那一段。
 *
 * 按"最接近"而不是"包含"：段落之间的行间空白、公式块在 bbox 上是断开的，
 * 严格包含会有一大片区域谁都挑不出来 —— 表现为滚到某处对侧突然不动了。
 */
export function nearestByY<T extends AnchoredSegment>(items: T[], page: number, y: number): T | null {
  let best: T | null = null
  let bestDist = Infinity
  for (const it of items) {
    if (it.page !== page) continue
    const cy = centerY(it.bbox)
    if (cy === null) continue
    const d = Math.abs(cy - y)
    if (d < bestDist) {
      bestDist = d
      best = it
    }
  }
  return best
}

// ---------------------------------------------------------------- 探针线判定

/** 滚动容器里一条东西的纵向量度（像素，相对容器顶边）。 */
export interface BandBox {
  id: string
  top: number
  bottom: number
}

/**
 * 探针线扫过的那一条。
 *
 * 不能退化成"候选里的第一条"：探针带只有容器高度的 8%（实测 512px 的面板上是 41px），
 * 而一条常有 100px 以上，于是**上一条的尾巴也压在带里**，按顺序取会稳定地慢一条 ——
 * 表现是高亮"总是差一行"。跨线的最多一条；万一线正好落在条间空隙里，退回离它最近的边。
 */
export function pickAtLine<T extends BandBox>(boxes: T[], line: number): T | null {
  let hit: T | null = null
  let hitTop = -Infinity
  let near: { box: T; d: number } | null = null
  for (const b of boxes) {
    if (b.top <= line && b.bottom >= line) {
      // 跨线的理论上唯一；真出现重叠就取更靠下的那条（后出现的那条才是阅读位置）
      if (b.top > hitTop) {
        hitTop = b.top
        hit = b
      }
    } else {
      const d = Math.min(Math.abs(b.top - line), Math.abs(b.bottom - line))
      if (!near || d < near.d) near = { box: b, d }
    }
  }
  return hit ?? near?.box ?? null
}
