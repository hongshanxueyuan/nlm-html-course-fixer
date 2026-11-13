# Studio contract fixtures

These synthetic fixtures contain no course data or credentials. They model the local server source, not an observed UAT response:

- `studio_concise.json` and `studio_empty_vertical.json`: `edx-platform` commit `ced8e2923b775dada561cc75fe34a836f5165722`, `cms/djangoapps/contentstore/views/course.py::_course_outline_json` and `views/item.py::create_xblock_info`. An empty vertical supports children but omits `child_info`.
- `studio_gallery_files.json`: `xblock-imagesgallery` commit `1feef1a30fdec72e77370eaf5f7dd76984826115`, `imagesgallery/imagesgallery.py::get_files` and `get_asset_json_from_content`.

When changing a fixture, check the corresponding source and update existing behavior tests. These contracts do not establish which version a deployed Studio uses.
