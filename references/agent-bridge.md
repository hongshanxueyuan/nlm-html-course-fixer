# 隔离子 Agent 交接

主 Agent 从正在运行的 `scripts.run_repair` 读取每一行 JSON。`agent_request` 包含 `split` 或 `review`、输入 `request` 路径和输出 `response` 路径。每个请求以 `fork_turns=none` 启动一个新 Agent，只传这两个路径、本文件的规则和 `nlm-course-slides/references/html-pagination-rules.md` 路径；不传其他小节或父对话内容。Agent 读取请求和该请求列出的本地图片，写 JSON 到临时文件后重命名为指定响应路径，避免半写入。批次按小节串行，主 Agent 在收到本请求响应后再处理下一请求；所有切分和复核 Agent 合计不得超过 3。主 Agent 持续等待程序的新请求或最终报告。

## split

请求包含本节原始 Markdown `source`、已按封面规则移除首行重复标题的 `body`、`cover_title`、旧分页 `old_pages`、按页顺序的 `images` 和固定图数 `count`。观察图片，以旧分页为主要主题线索，对 `body` 按原有顺序选择连续文本片段；不得改写、补写或遗漏。旧稿漏掉的原文按主题并入相邻页，标题、段落、列表和表格尽量保持完整；图数与全文覆盖优先于结构偏好。封面固定为 `# <cover_title>`，正文不能重新插入这个已去重的首标题。输出：

```json
{"pages": ["# 标题", "第一段原文", "第二段原文"]}
```

数组长度严格等于 `count`；封面后每页非空，依次连接覆盖完整 `body`。完成条件是响应文件存在且仅包含该小节结果。

## review

由另一名新 Agent 读取同一个小节的原文、旧分页、图片和候选 `pages`，逐页检查主题与边界；对于没有专属画面的遗漏原文，允许其按顺序并入相邻页。输出 `{"approved": true}` 或 `{"approved": false}`，不得改写候选页。完成条件是独立的响应文件存在；程序负责客观检查全文覆盖，语义复核不是对图片字节或语音质量的证明。
