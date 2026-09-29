# CRAG 检索质量评估（Corrective RAG）

判断检索到的上下文**够不够支撑回答**，给出三级标签。

## 三档

| 标签 | 判据 | 后续动作 |
|---|---|---|
| `relevant` | 至少 2 个片段**直接**包含回答问题所需信息 | 直接生成 |
| `ambiguous` | 有相关内容但**关键信息缺失**（缺数值、缺该章节、答非所问） | Query Rewrite 重试（上限 3 轮） |
| `irrelevant` | 全部片段与问题无关，或检索为空 | Web Search 兜底 |

## 判断要点

1. **看是否"直接回答"**，不是"话题相关"。问"哪年发布的"但片段没提年份 → `ambiguous`，不是 `relevant`。
2. 只要有一档更差的可能，就**往低判**。宁可多跑一轮 Query Rewrite，也不要拿半截材料硬生成。
3. `ambiguous` 时**必须指出缺什么**，这是 Query Rewrite 的输入。写"检索结果不够好"是废话。
4. 已重写 3 轮仍 `ambiguous` → 上报 `irrelevant`，不要再重写。

## 输出

```json
{
  "level": "ambiguous",
  "missing": "缺少 2024 年之后的实验数据，检索到的片段全部截止 2023 年",
  "rewrite_hint": "加入 '2024 OR 2025'、具体会议名（ICLR/NeurIPS），并改用英文检索"
}
```

`level` 为 `relevant` 时 `missing` 留空字符串。

## 示例

问："MoE 的 aux loss 系数取多少合适？"
检索到：MoE 结构介绍、路由算法、训练技巧章节
→ `ambiguous`（讲到了 MoE，但没有任何关于 aux loss 系数的内容），
`missing="未检索到 aux loss 系数的取值讨论"`，
`rewrite_hint="改用 'load balancing loss coefficient'、'auxiliary loss weight' 等更精确的英文表述"`

问："这篇的标题是什么？"
检索到：该论文摘要片段，含标题
→ `relevant`
