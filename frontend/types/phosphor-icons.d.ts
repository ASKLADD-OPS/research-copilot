/**
 * `@phosphor-icons/vue@2.2.1` 的 package.json 里 `types` 指向 `dist/index.d.ts`，
 * 但发包时那个文件没进 tarball（dist 下只有 .mjs），所以 TS 报 TS7016。
 * 这里补一份最小声明，属性按 Phosphor 的 IconProps 写。
 *
 * 代价：**新增图标要在这里补一行**。换来的是 `:size` / `weight` 这些 props 仍然
 * 受类型约束，而不是整个模块退化成 any。
 */
declare module '@phosphor-icons/vue' {
  import type { DefineComponent } from 'vue'

  export interface IconProps {
    /** 边长，px 或带单位的字符串 */
    size?: number | string
    color?: string
    weight?: 'thin' | 'light' | 'regular' | 'bold' | 'fill' | 'duotone'
    mirrored?: boolean
    alt?: string
  }

  export type PhosphorIcon = DefineComponent<IconProps>

  export const PhArrowSquareOut: PhosphorIcon
  export const PhArrowsClockwise: PhosphorIcon
  export const PhCaretRight: PhosphorIcon
  export const PhDownloadSimple: PhosphorIcon
  export const PhFilePdf: PhosphorIcon
  export const PhFunnel: PhosphorIcon
  export const PhMagnifyingGlass: PhosphorIcon
  export const PhMoon: PhosphorIcon
  export const PhPaperPlaneRight: PhosphorIcon
  export const PhPencilSimple: PhosphorIcon
  export const PhPlus: PhosphorIcon
  export const PhStop: PhosphorIcon
  export const PhSun: PhosphorIcon
  export const PhTrash: PhosphorIcon
  export const PhUploadSimple: PhosphorIcon
}
