# Studio 接口契约

涉及课程结构、组件读取或 ImageGallery 读写时，对照本页和当前部署对应版本的服务端源码。下列事实来自本地源码，不证明线上部署版本一致。

## 课程与组件读取

- `GET /course/{course_id}?format=concise` 用于发现结构，不包含组件正文；`has_children` 表示组件支持子项，不代表当前子项非空。空单元可省略 `child_info`。见 [course.py](../../../edx-platform/cms/djangoapps/contentstore/views/course.py) 的 `_course_outline_json`、[item.py](../../../edx-platform/cms/djangoapps/contentstore/views/item.py) 的 `create_xblock_info` 和 [vertical_block.py](../../../edx-platform/common/lib/xmodule/xmodule/vertical_block.py)。
- `GET /xblock/{locator}` 返回 `data` 和显式设置的 `metadata`；部分 XBlock 的其他内容字段不在返回值中。`edited_on` 是面向界面的时间，不应作为精确版本。见 [item.py](../../../edx-platform/cms/djangoapps/contentstore/views/item.py) 的 `xblock_handler`、`_get_module_info`。
- 仅对目标 vertical 调用 `publish: make_public`；本地服务端的 `publish` 复制该节点的子树。通过课程发布结构中该 vertical 的 `published`、`has_changes` 回读结果，父 sequential 须已有发布版本；兄弟单元的状态不参与目标单元的发布判断。见 [item.py](../../../edx-platform/cms/djangoapps/contentstore/views/item.py) 的 `_save_xblock` 和 [split_draft.py](../../../edx-platform/common/lib/xmodule/xmodule/modulestore/split_mongo/split_draft.py) 的 `publish`。
- 结构或内容无法完整核验时保留未确认状态。读后写检查不是原子锁；同名静态资源的文件内容也不能仅凭名称证明相同。

## ImageGallery

`get_files` 返回 `files` 和 `total_count`，文件含 `id`、`asset_key` 和 `display_name`；`get_audios` 返回 `audios`；`save_audio` 按 `document_id`、`file_name` 替换单条绑定；`update_play_config` 替换完整配置。相关设置包含 `contents`、`content_names`、`audio_contents` 和 `play_config`。见 [imagesgallery.py](../../../blocks/xblock-imagesgallery/imagesgallery/imagesgallery.py)。

离线测试样例位于本仓库 `tests/fixtures/`，由上述本地源码构造，不是真实课程响应。更新契约时同步检查样例、生产适配器和测试。

空单元及组件版本的只读接口调查见 [studio-read-survey.md](studio-read-survey.md)。
