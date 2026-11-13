import copy
import json
import subprocess
from pathlib import Path

from scripts.repair_one import repair_one

SEQUENTIAL = "block-v1:FIRAx+TARGET+20261113+type@sequential+block@section"
VERTICAL = "block-v1:FIRAx+TARGET+20261113+type@vertical+block@unit"
GALLERY = "block-v1:FIRAx+TARGET+20261113+type@imagesgallery+block@gallery"
SUMMARY = "block-v1:FIRAx+TARGET+20261113+type@html+block@summary"

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def package_at(root):
    write_json(root / "course-content.json", {
        "course_id": "course-v1:FIRAx+SOURCE+20261113", "chapters": [{"name": "第一章", "sections": [{
            "name": "1.2 测试", "verticals": [
                {"name": "赋能内容", "blocks": [
                    {"category": "html", "name": "文字讲解", "text": "<h1>测试</h1><p>第一段。</p><p>第二段。</p>"},
                    {"category": "html", "name": "本节要点", "text": "不属于正文"},
                ]},
                {"name": "在线训战", "blocks": [{"category": "html", "name": "文字讲解", "text": "错误正文"}]},
            ],
        }]}],
    })
    write_json(root / "section-list.json", {"sections": [{
        "section_id": "1.2", "section_title": "1.2 测试", "block_type": "html",
        "page_content": ["# 测试", "改写的旧正文", "旧稿遗漏结尾"],
    }]})
    gallery = root / "1.2 测试" / "imagesgallery"
    for page in range(1, 4):
        path = gallery / "images" / f"{page}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image")
    write_json(gallery / "imagesgallery.json", {
        "section_id": "1.2", "section_title": "1.2 测试", "block_type": "html",
        "audio": {"voice": "voice-old", "model": "model-old", "rate": 1.1, "gap_seconds_between_segments": 0.5},
        "items": [{"page": n, "image": f"images/{n}.jpg", "speech": "旧音频正文", "subtitle": "旧字幕"} for n in range(1, 4)],
    })


class Studio:
    def __init__(self):
        self.config = {"show_subtitles": True, "unrelated_setting": "keep", "time_config": [{"page": 1, "subtitle": "old"}]}
        self.audios = [{"document_id": "old.mp3", "file_name": "old.mp3"}]
        self.published = False

    def get_course_structure(self, course_id):
        return {"category": "course", "child_info": {"children": [
            {"category": "chapter", "display_name": "第一章", "child_info": {"children": [
                {"category": "sequential", "display_name": "1.2 测试", "id": SEQUENTIAL, "child_info": {"children": [
                    {"category": "vertical", "display_name": "赋能内容", "id": VERTICAL, "child_info": {"children": [
                        {"category": "imagesgallery", "id": GALLERY},
                        {"category": "html", "display_name": "本节要点", "id": SUMMARY},
                    ]}},
                ]}},
            ]}},
        ]}}

    def get_block_details(self, locator):
        category = locator.split("+type@", 1)[1].split("+", 1)[0] if "+type@" in locator else ("vertical" if locator == "other-unit" else "html")
        return {"id": locator, "category": category, "data": "", "metadata": {}}

    def get_files(self, locator):
        return {"files": [{"id": f"asset-{n}", "filename": f"{n}.jpg"} for n in range(1, 4)], "total_count": 3}

    def get_play_config(self, locator):
        return {"play_config": copy.deepcopy(self.config)}

    def get_audios(self, locator):
        return {"audios": copy.deepcopy(self.audios)}

    def upload_audio(self, locator, audio):
        return {"document_id": "new.mp3", "file_name": "new.mp3"}

    def bind_audio(self, locator, binding):
        self.audios = [copy.deepcopy(binding)]

    def update_play_config(self, locator, config):
        self.config = copy.deepcopy(config)

    def publish_sequential(self, locator):
        self.published = True

    def get_course_publication_structure(self, course_id):
        return {SEQUENTIAL: {"published": self.published, "has_changes": not self.published}}


def synthesize(manifest, voice, output, expected_pages=None):
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert voice == {"voice": "voice-old", "model": "model-old", "rate": 1.1, "gap_seconds_between_segments": 0.5}
    assert [item["speech"] for item in data["items"]] == (expected_pages or ["# 测试", "第一段。", "第二段。"])
    output.mkdir(parents=True, exist_ok=True)
    audio = output / "full_speech.mp3"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "3", str(audio)], check=True)
    for index, item in enumerate(data["items"]):
        item["subtitle"] = item.pop("speech")
        item["start"], item["end"] = index * 1000, (index + 1) * 1000
    data["audio"] = {**voice, "full_audio": str(audio)}
    write_json(manifest, data)
    return audio


def test_single_section_repairs_original_text_and_publishes_in_place(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    original = {p.relative_to(package): p.read_bytes() for p in package.rglob("*") if p.is_file()}
    studio = Studio()
    split_inputs = []
    review_inputs = []

    def split(request):
        split_inputs.append(request)
        return ["# 测试", "第一段。", "第二段。"]

    def review(request):
        review_inputs.append(request)
        return True

    result = repair_one(package, "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113", "1.2", package / "repair", split=split, review=review, synthesize=synthesize, studio=studio)

    assert result["status"] == "success"
    assert studio.published and studio.audios == [{"document_id": "new.mp3", "file_name": "new.mp3"}]
    assert studio.config["unrelated_setting"] == "keep"
    assert [item["subtitle"] for item in studio.config["time_config"]] == ["# 测试", "第一段。", "第二段。"]
    assert len(split_inputs) == len(review_inputs) == 1
    assert all((package / path).read_bytes() == data for path, data in original.items())
    output = json.loads((package / "repair" / "1.2" / "imagesgallery.json").read_text(encoding="utf-8"))
    assert all((package / "repair" / "1.2" / item["image"]).resolve().is_file() for item in output["items"])


def test_target_course_identity_mismatch_does_not_write(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    studio = Studio()
    result = repair_one(package, "https://studio.example.test/course/course-v1:FIRAx+OTHER+20261113", "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=synthesize, studio=studio)
    assert result["status"] == "failed"
    assert not studio.published
    assert studio.audios == [{"document_id": "old.mp3", "file_name": "old.mp3"}]


def test_failed_subtitle_write_restores_old_audio_without_publishing(tmp_path):
    class FailedSubtitleStudio(Studio):
        def update_play_config(self, locator, config):
            raise TimeoutError("request failed")

    package = tmp_path / "package"
    package_at(package)
    studio = FailedSubtitleStudio()
    old_config = copy.deepcopy(studio.config)
    result = repair_one(package, "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113", "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=synthesize, studio=studio)
    assert result["status"] == "failed"
    assert result["recovery"] == "restored and verified"
    assert studio.audios == [{"document_id": "old.mp3", "file_name": "old.mp3"}]
    assert studio.config == old_config and not studio.published


def test_timeout_after_binding_reads_current_state_before_retry(tmp_path):
    class TimedOutBindingStudio(Studio):
        def bind_audio(self, locator, binding):
            super().bind_audio(locator, binding)
            raise TimeoutError("response lost")

    package = tmp_path / "package"
    package_at(package)
    studio = TimedOutBindingStudio()
    result = repair_one(package, "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113", "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=synthesize, studio=studio)
    assert result["status"] == "success"
    assert studio.audios == [{"document_id": "new.mp3", "file_name": "new.mp3"}] and studio.published


def test_repair_output_is_not_mistaken_for_original_gallery_on_rerun(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    kwargs = dict(split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=synthesize)
    first = repair_one(package, "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113", "1.2", package / "repair", studio=Studio(), **kwargs)
    second = repair_one(package, "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113", "1.2", package / "repair", studio=Studio(), **kwargs)
    assert first["status"] == second["status"] == "success"


def test_changed_unit_is_not_published_after_update(tmp_path):
    class ChangedStudio(Studio):
        def __init__(self):
            super().__init__()
            self.reads = 0

        def get_course_structure(self, course_id):
            self.reads += 1
            tree = super().get_course_structure(course_id)
            if self.reads >= 3:
                unit = tree["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]
                unit["child_info"]["children"].append({"category": "html", "id": "another-editor"})
            return tree

    package = tmp_path / "package"
    package_at(package)
    studio = ChangedStudio()
    result = repair_one(package, "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113", "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=synthesize, studio=studio)
    assert result["status"] == "unconfirmed"
    assert not studio.published


def test_new_sibling_unit_during_audio_generation_is_not_published(tmp_path):
    class ChangedSectionStudio(Studio):
        changed = False

        def get_course_structure(self, course_id):
            tree = super().get_course_structure(course_id)
            section = tree["child_info"]["children"][0]["child_info"]["children"][0]
            if self.changed:
                section["child_info"]["children"].append({
                    "category": "vertical", "display_name": "并发新增", "id": "other-unit",
                    "child_info": {"children": []},
                })
            return tree

    package = tmp_path / "package"
    package_at(package)
    studio = ChangedSectionStudio()

    def concurrent_synthesis(manifest, voice, output):
        audio = synthesize(manifest, voice, output)
        studio.changed = True
        return audio

    result = repair_one(package, "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113", "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=concurrent_synthesis, studio=studio)
    assert result["status"] != "success"
    assert not studio.published


def test_transient_target_structure_read_is_retried_before_writing(tmp_path):
    class TransientRead(Studio):
        attempts = 0

        def get_course_structure(self, course_id):
            self.attempts += 1
            if self.attempts == 1:
                raise TimeoutError("temporary read failure")
            return super().get_course_structure(course_id)

    package = tmp_path / "package"
    package_at(package)
    studio = TransientRead()
    result = repair_one(package, "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113", "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=synthesize, studio=studio)
    assert result["status"] == "success" and studio.published


def test_changed_sibling_component_metadata_is_not_published(tmp_path):
    class ChangedComponentStudio(Studio):
        changed = False

        def get_course_structure(self, course_id):
            tree = super().get_course_structure(course_id)
            unit = tree["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]
            if self.changed:
                unit["child_info"]["children"][1]["display_name"] = "并发修改"
            return tree

    package = tmp_path / "package"
    package_at(package)
    studio = ChangedComponentStudio()

    def concurrent_synthesis(manifest, voice, output):
        audio = synthesize(manifest, voice, output)
        studio.changed = True
        return audio

    result = repair_one(package, "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113", "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=concurrent_synthesis, studio=studio)
    assert result["status"] != "success" and not studio.published


def test_source_course_json_can_use_original_export_filename(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    (package / "course-content.json").rename(package / "fira_course-v1_FIRAx_SOURCE_20261113.json")
    studio = Studio()
    result = repair_one(package, "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113", "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=synthesize, studio=studio)
    assert result["status"] == "success"


def test_retries_bad_pages_and_keeps_table_and_repeated_heading(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    source_path = package / "course-content.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["chapters"][0]["sections"][0]["verticals"][0]["blocks"][0]["text"] = "<h1>测试</h1><h2>重复</h2><p>第一段。</p><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table><h2>重复</h2><p>第二段。</p>"
    write_json(source_path, source)
    pages = ["# 测试", "## 重复\n\n第一段。\n\n| A | B |\n| --- | --- |\n| 1 | 2 |", "## 重复\n\n第二段。"]
    attempts = []
    reviews = []

    def split(request):
        attempts.append(1)
        return ["# 测试", "改写的旧正文", "第二段。"] if len(attempts) == 1 else pages

    def review(request):
        reviews.append(1)
        return len(reviews) > 1

    studio = Studio()
    result = repair_one(package, "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113", "1.2", package / "repair", split=split, review=review, synthesize=lambda manifest, voice, output: synthesize(manifest, voice, output, pages), studio=studio)
    assert result["status"] == "success"
    assert len(attempts) == 3 and len(reviews) == 2
    assert [row["subtitle"] for row in studio.config["time_config"]] == pages


def test_accepts_studio_file_name_and_asset_key_shape(tmp_path):
    class NamedStudio(Studio):
        def get_files(self, locator):
            return {"files": [{"asset_key": f"asset-{n}", "display_name": f"{n}.jpg"} for n in range(1, 4)], "total_count": 3}

    package = tmp_path / "package"
    package_at(package)
    studio = NamedStudio()
    result = repair_one(package, "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113", "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=synthesize, studio=studio)
    assert result["status"] == "success"


def test_failed_audio_restore_is_reported_as_unconfirmed(tmp_path):
    class FailedRestoreStudio(Studio):
        def update_play_config(self, locator, config):
            raise TimeoutError("write failed")

        def bind_audio(self, locator, binding):
            if binding["document_id"] == "old.mp3":
                raise TimeoutError("restore failed")
            super().bind_audio(locator, binding)

    package = tmp_path / "package"
    package_at(package)
    studio = FailedRestoreStudio()
    result = repair_one(package, "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113", "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=synthesize, studio=studio)
    assert result["status"] == result["recovery"] == "unconfirmed"
    assert not studio.published


def test_uncovered_original_text_never_reaches_tts_or_studio(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    studio = Studio()
    generated = []
    result = repair_one(package, "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113", "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "遗漏结尾"], review=lambda request: True, synthesize=lambda *args: generated.append(True), studio=studio)
    assert result["status"] == "failed" and result["phase"] == "pagination"
    assert not generated and not studio.published
