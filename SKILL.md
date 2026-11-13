---
name: nlm-html-course-fixer
description: 修复 HTML 课程的逐页讲解、配音和字幕。
disable-model-invocation: true
---

# 修复一个 HTML 小节

本流程会上传音频、修改 Gallery 并发布整个目标小节，包括其他单元中已有的未发布修改。只在用户明确启动课程修复后执行。输入是本地课程包、明确的目标 Studio 课程 URL 和一个 `section_id`；目标 URL 决定写入课程，来源课程只用于读取。首次真实 UAT 仅处理用户选定的一门课程的第一章，结束后由用户试听音频。

## 准备

1. 找到 `nlm-course-slides`、`ppt-to-imagesgallery`、`nlm-course-auditor` 和 `studio-imagegallery-publish` 的本地 checkout；通过同级目录或环境变量 `NLM_COURSE_SLIDES_ROOT`、`PPT_TO_IMAGESGALLERY_ROOT`、`NLM_COURSE_AUDITOR_ROOT`、`STUDIO_IMAGEGALLERY_PUBLISH_ROOT` 指定。可用 Python、`markdownify`、`requests`、`bl`、`ffmpeg`、`ffprobe` 和 OSS SDK；Studio tenant 配置须唯一匹配目标 URL，认证用现有环境变量，不写入产物。
2. 读取现有 `bailian-protocol`，完成其版本与鉴权预检。沿用课程原有的百炼 TTS 配置；正式运行中使用用户已选定的修复范围，不插入逐页审核关卡。
3. 在课程包下选一个专用空目录作为 `--repair-dir`。原课程包文件只读；所有产物、模型交接和报告存入该目录。若目标、来源或声音配置不能唯一确定，报告失败，不猜测。

## 执行

在本 skill 根目录启动下面的命令（将尖括号替换为实际路径和输入），保留进程并读取其标准输出：

```text
python -m scripts.run_repair --package <课程包> --studio-url <目标课程URL> --section <小节ID> --repair-dir <专用修复目录> --tenant-config <Studio租户配置JSON>
```

程序输出 `agent_request` 后，按 [隔离子 Agent 交接](references/agent-bridge.md) 启动对应的独立子 Agent，等它原子写入响应，再继续读取进程输出。切分与复核各由不同的隔离子 Agent 执行，所有运行中的切分及复核 Agent 合计不超过 3。每次重试都创建新的隔离 Agent。

程序确认固定图数、连续页码、原文覆盖和独立复核后，使用修复清单重新合成全部页面，验证 MP3 和时间轴，再对实际 Gallery 的图片元数据、音频身份、完整播放设置及小节状态逐项回读。同名同序且数量一致时原位更新；该比较不证明图片字节相同。若已知同名图片内容不一致，在命令末尾加 `--force-replace`。图片不一致时先用隔离名称上传本地图，完整回读新 Gallery 后替换旧 Gallery，保留其他组件及顺序。写入异常先回读对账；无法安全确认或恢复时不发布并报告实际状态。

## 完成

读取 `<专用修复目录>/<小节ID>/report.json`，报告目标链接、阶段、成功或失败或未确认、恢复结果，以及图片仅按名称、数量和顺序对照的限制。替换时 `image-map.json` 记录源图、隔离副本和上传资产 ID，`replacement-state.json` 保留待对账阶段；存在该状态时单节入口暂不重复创建。实际运行涉及 `bl speech synthesize` 时同时说明配音结果。真实 UAT 与模拟测试分开报告，运行结束后提醒用户试听音频；不自动调用语音识别。
