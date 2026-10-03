/**
 * 段落 ↔ PDF 位置的对齐数学自检。
 *
 * 只覆盖 `utils/ui.ts` 里那两个**纯函数**（bbox → 中心、按页挑最近段）：它们决定
 * "右栏高亮哪一段"，算错的表现是**不报错但高亮错段**，属于最难靠肉眼发现的一类。
 * 滚动/IntersectionObserver 那部分依赖真实布局，交给浏览器，不在这里假装测过。
 *
 * 运行：`npm run check:sync`（直接跑 .ts 依赖 Node 的类型剥离，无构建步骤）
 */
import assert from 'node:assert/strict'

import { centerY, nearestByY, pickAtLine, rectsFromBbox } from '../utils/ui.ts'

/** 归一化坐标是浮点数，等值比较必然踩到 0.1+0.2 那套，用 1e-9 容差。 */
function near(actual, expected, msg) {
  assert.ok(Math.abs(actual - expected) < 1e-9, msg ?? `期望 ${expected}，实际 ${actual}`)
}

// ---------------------------------------------------------------- bbox → 中心
near(centerY([0.1, 0.2, 0.9, 0.4]), 0.3)
// 角点写反（右下 → 左上）也要算出同一个中心，后端不同解析器给出的顺序不一定一致
near(centerY([0.9, 0.4, 0.1, 0.2]), 0.3)
near(centerY({ page: 3, boxes: [[0, 0.5, 1, 0.7]] }), 0.6)
assert.equal(centerY(null), null)
assert.equal(rectsFromBbox({ page: 1, boxes: [] }).length, 0)

// ---------------------------------------------------------------- 按页挑最近段
const segs = [
  { id: 'a', page: 1, bbox: [0.05, 0.05, 0.95, 0.15] }, // 中心 0.10
  { id: 'b', page: 1, bbox: [0.05, 0.3, 0.95, 0.4] }, // 中心 0.35
  { id: 'c', page: 2, bbox: [0.05, 0.8, 0.95, 0.9] }, // 中心 0.85，另一页
  { id: 'd', page: 1, bbox: null }, // 没有坐标
]

assert.equal(nearestByY(segs, 1, 0).id, 'a')
assert.equal(nearestByY(segs, 1, 0.34).id, 'b')
// 视口滚到页尾时，只能在本页里挑，不能因为 c 更"接近"就跳页
assert.equal(nearestByY(segs, 1, 0.99).id, 'b')
assert.equal(nearestByY(segs, 2, 0.5).id, 'c')
// 该页没有任何带坐标的段 → null（前端原地不动，而不是乱跳到别页）
assert.equal(nearestByY(segs, 3, 0.5), null)
// page 缺失的段不参与匹配，否则会把没有坐标的内容也当成可定位目标
assert.equal(nearestByY(segs.filter((s) => !s.page), 1, 0.5), null)

console.log('scroll-sync 自检通过：bbox→中心 5 项、按页挑段 6 项')

// ---------------------------------------------------------------- 探针线判定
// 这组数字抄自真机实测：512px 的面板、一条 123px 高、带 8% 高（41px）。
// 带只有 41px 而条高 123px，**上一条的尾巴必然也压在带里** —— 曾经的实现取"带内顺序
// 第一个"，于是永远慢一条（滚到第 4 段高亮的是第 3 段）。这组用例就是钉死这一点。
const H = 512
const LINE = H * 0.12 // 阅读线 = 带正中 = 61.44px
const tall = (id, top) => ({ id, top, bottom: top + 123 })

// 目标条顶边摆在阅读线上：它跨线，上一条的尾巴也在带里
assert.equal(pickAtLine([tall('prev', -70), tall('cur', LINE)], LINE).id, 'cur')
// 顺序反过来（IO 回调不保证顺序）结论必须一样
assert.equal(pickAtLine([tall('cur', LINE), tall('prev', -70)], LINE).id, 'cur')
// 三条同时入带（带被拉宽或条很矮时会发生）：取最靠下、跨线的那条
assert.equal(pickAtLine([tall('a', -200), tall('b', 10), tall('c', LINE)], LINE).id, 'c')
// 线正好落在条间空隙（8px gutter）：没有跨线的，退回离阅读线最近的边
assert.equal(pickAtLine([tall('above', LINE - 131), tall('below', LINE + 8)], LINE).id, 'above')
// 空候选 / 全在远处
assert.equal(pickAtLine([], LINE), null)
assert.equal(pickAtLine([tall('far', 4000)], LINE).id, 'far')
// 顶边与阅读线严格相等这种浮点边界也要算"跨线"，否则目标条会自己掉出判定
assert.equal(pickAtLine([tall('edge', LINE)], LINE).id, 'edge')

console.log('scroll-sync 自检通过：探针线判定 7 项')
