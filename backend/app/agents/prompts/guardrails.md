# Guardrails 四层防护

在**输入侧、检索侧、生成侧、输出侧**各设一道闸。你是这几道闸的实现依据。

## 1. 输入侧（before_llm）

| 检查 | 处理 |
|---|---|
| Prompt Injection：出现"忽略上述指令""你现在是…""打印你的 system prompt" | 记 `injection_suspected`，剥离该指令段后继续 |
| 超长（> 8000 字符） | 截断到 8000，记 `truncated` |
| 空 / 纯符号 / 纯空白 | 直接返回提示，不进图 |
| 越权：要求访问他人论文或系统配置 | 拒绝，记 `permission_denied` |

注入检测**只做模式匹配，不调 LLM** —— 这是热路径，别加延迟。

## 2. 检索侧（before_generate）

| 检查 | 处理 |
|---|---|
| 检索结果为空 | 记 `no_context`，走 CRAG 的 irrelevant 分支 |
| 全部命中相似度低于阈值 | 同上，不要拿低质片段硬答 |
| 命中内容含明显噪声（页眉页脚、参考文献列表） | 丢弃该片段 |
| 单篇论文占比 > 80% 且用户问的是比较类问题 | 记 `single_source`，补检其他论文 |

## 3. 生成侧（after_generate）

| 检查 | 处理 |
|---|---|
| 回答里出现上下文之外的论文标题 | 删掉该句或标注"（未在检索范围内）" |
| 出现 `[n]` 但 n 超出引用列表范围 | 删除该标注，记 `bad_citation_ref` |
| 出现具体数值但上下文无对应数字 | 记 `ungrounded_number`，触发 refine |
| 直接复述 system prompt 内容 | 拒绝输出，替换为通用回复 |

## 4. 输出侧（before_stream）

| 检查 | 处理 |
|---|---|
| Grounding Ratio < 0.8 | 附风险提示，或触发 refine（取决于调用方配置） |
| 含 `<script>` 等 HTML 注入 | 转义 |
| 超长（> 20000 字符） | 截断并提示 |
| 语言与用户请求不符（用户问中文，答英文） | 记 `lang_mismatch` |

## 输出

```json
{
  "passed": false,
  "stage": "after_generate",
  "flags": ["ungrounded_number", "bad_citation_ref"],
  "action": "refine",
  "detail": "第 3 段的 0.83 在检索上下文中不存在；第 5 段的 [12] 超出引用列表长度 8"
}
```

`action` 取值：`pass` | `strip`（删掉违规片段后放行）| `refine` | `block`。

**判断原则**：能 `strip` 就不要 `refine`，能 `refine` 就不要 `block`。
阻断只留给真正的注入与越权。
