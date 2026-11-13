# Studio 只读接口调查

依据本地 `edx-platform` 提交 `ced8e2923b775dada561cc75fe34a836f5165722` 和 `xblock-imagesgallery` 提交 `1feef1a30fdec72e77370eaf5f7dd76984826115` 的源码；未访问目标课程，也未验证线上部署版本。

## 已确认的读取边界

| 读取 | 可确认的信息 | 限制 |
| --- | --- | --- |
| `GET /course/{course_id}?format=concise` | 递归课程结构、组件类别和 locator | 空容器与缺失的子项列表都可能表现为省略 `child_info`；`has_children` 表示支持子项，不是子项数量 |
| `GET /xblock/{locator}` | `data` 和显式设置的 `metadata` | 不包含所有独立 `Scope.content` 字段；`edited_on` 是格式化的显示时间，不是精确版本 |
| `GET /xblock/outline/{locator}`、`GET /xblock/container/{locator}` | 面向课程大纲或单元页面的信息 | 本地实现不提供可供此流程确认空 vertical 的完整直接子项列表 |
| Gallery 的 `get_files`、`get_audios`、`get_play_config` | 对应 Gallery 的图片元数据、音频绑定、播放配置 | 文件名和资产 ID 不证明静态文件字节未变化 |

证据：[`course.py::_course_outline_json`](../../../edx-platform/cms/djangoapps/contentstore/views/course.py)、[`item.py::create_xblock_info`、`_get_module_info`、两个 XBlock 处理器](../../../edx-platform/cms/djangoapps/contentstore/views/item.py)、[`get_default_time_display`](../../../edx-platform/common/djangoapps/util/date_utils.py)。视频的 `enable_watermark` 是独立 `Scope.content` 字段，见 [video_xfields.py](../../../edx-platform/common/lib/xmodule/xmodule/video_module/video_xfields.py)；通用 `/xblock` 的 `data` 和 `metadata` 因此不足以核对完整视频状态。

在已检查的接口中没有找到同时提供明确空子项列表和所有组件可靠内容版本的读取方式。这不是对其他服务或线上版本的穷尽结论。当前修复器继续对无法完整确认的单元或组件返回未确认。

## 后续接口需求

若要解除该限制，应先确认部署端是否已有满足以下条件的只读接口；没有时再考虑扩展 Studio：

1. 针对获准读取的目标，明确返回直接子项列表，包括空列表，并绑定被读取的组件身份。
2. 提供能随相关 `Scope.content`、`Scope.settings` 和子项变化而变化的可靠版本或摘要；明确默认值、外部资源和字段序列化范围，不依赖分钟级显示时间。
3. 用服务端集成测试验证空单元、独立内容字段变化、访问权限和版本稳定性；适配器遇到接口缺失、无效或版本不匹配时仍停止。

该接口只能改善读回对账。要消除读后写竞态，还需要服务端提供可条件化的发布操作；静态资源字节及线上部署版本仍须分别确认。
