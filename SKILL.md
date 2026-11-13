---
name: nlm-html-course-fixer
description: 修复 HTML 课程的逐页讲解、配音和字幕。
disable-model-invocation: true
---

# 修复 HTML 课程小节

本流程会上传音频、修改 Gallery 并发布整个目标小节，包括其他单元中已有的未发布修改。只在用户明确启动课程修复后执行。输入是本地课程包、明确的目标 Studio 课程 URL，以及可选的章节或小节范围；目标 URL 决定写入课程，来源课程只用于读取。首次真实 UAT 仅处理用户选定的一门课程的第一章，结束后由用户试听音频。

## 准备

1. 找到 `nlm-course-slides`、`ppt-to-imagesgallery`、`nlm-course-auditor` 和 `studio-imagegallery-publish` 的本地 checkout；通过同级目录或环境变量 `NLM_COURSE_SLIDES_ROOT`、`PPT_TO_IMAGESGALLERY_ROOT`、`NLM_COURSE_AUDITOR_ROOT`、`STUDIO_IMAGEGALLERY_PUBLISH_ROOT` 指定。可用 Python、`markdownify`、`requests`、`bl`、`ffmpeg`、`ffprobe` 和 OSS SDK；Studio tenant 配置须唯一匹配目标 URL，认证用现有环境变量，不写入产物。
2. 读取现有 `bailian-protocol`，完成其版本与鉴权预检。沿用课程原有的百炼 TTS 配置；正式运行中使用用户已选定的修复范围，不插入逐页审核关卡。
3. 在课程包下选一个专用空目录作为 `--repair-dir`；省略时使用课程包下的 `nlm-html-course-fixer`。续跑使用同一目录。原课程包文件只读；所有产物、模型交接和报告存入该目录。若目标、来源或声音配置不能唯一确定，报告失败，不猜测。

## 执行

在本 skill 根目录启动下面的命令（将尖括号替换为实际路径和输入），保留进程并读取其标准输出：

```text
python -m scripts.run_repair --package <课程包> --studio-url <目标课程URL> --first-chapter --tenant-config <Studio租户配置JSON>
```

首章以外的用户选定范围使用 `--chapter <章节完整名称>` 或一个或多个 `--section <小节ID>`；用户明确选择整课时省略范围参数。按用户范围执行，不自行扩大。程序串行处理小节，同一时间最多一个切分或复核请求。程序输出 `agent_request` 后，按 [隔离子 Agent 交接](references/agent-bridge.md) 启动新的隔离子 Agent（`fork_turns=none`），等它原子写入响应，再继续读取进程输出。切分与复核分别由不同的 Agent 执行，每次重试创建新的 Agent。

程序确认固定图数、连续页码、原文覆盖和独立复核后，使用修复清单重新合成全部页面，验证 MP3 和时间轴，再对实际 Gallery 的图片元数据、音频身份、完整播放设置及小节状态逐项回读。同名同序且数量一致时原位更新；该比较不证明图片字节相同。若已知同名图片内容不一致，在命令末尾加 `--force-replace`。图片不一致时先用隔离名称上传本地图，完整回读新 Gallery 后替换旧 Gallery，保留其他组件及顺序。写入异常先回读对账；无法安全确认或恢复时不发布并报告实际状态。创建响应未提供新 Gallery 身份时，即使线上出现新块也不能认定归属，报告未确认。读后写检查并非原子锁，无法排除检查后的并发变化。

发布前核对同一小节中其他组件可读取的正文和配置：当前支持 HTML、problem 和本地接口契约已覆盖的 ImageGallery；视频及其他无法确认全部内容字段的组件，或不完整的单元结构，报告未确认并停止该小节。此检查不验证其他组件共用静态资产的文件内容，也不能证明线上 XBlock 版本与本地源码一致。新 Gallery 及已确认身份的创建中断，仅在核对出本地契约的空图片、音频、播放配置及元数据默认值后继续。

## 完成

读取 `<专用修复目录>/batch-report.json` 和各小节 `report.json`，报告所选范围、目标链接、阶段、成功或失败或未确认、恢复结果，以及图片仅按名称、数量和顺序对照的限制。续跑重新核验来源、图片、音色、生成清单与 MP3 的摘要及线上状态；只复用核验通过的成果。替换时 `image-map.json` 记录源图、隔离副本和上传资产 ID，`replacement-state.json` 保留待对账阶段。创建或音频登记的结果无法从线上唯一确认时报告未确认，保留状态供后续排查。实际运行涉及 `bl speech synthesize` 时同时说明配音结果。真实 UAT 与模拟测试分开报告，运行结束后提醒用户试听音频；不自动调用语音识别。
