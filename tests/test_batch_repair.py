from scripts.batch_repair import run_batch
import pytest
import json
import copy
import shutil
import subprocess
from pathlib import Path
from test_repair_one import Studio, package_at, synthesize
from test_repair_one import GALLERY as OLD_GALLERY, SEQUENTIAL, VERTICAL
from test_gallery_replacement import ReplacementStudio, GALLERY, NEW_GALLERY


TARGET = "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113"


def test_batch_repairs_selected_section_and_reports_range(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    studio = Studio()
    report = run_batch(
        package, TARGET, package / "repair", sections=["1.2"], studio=studio,
        split=lambda request: ["# 测试", "第一段。", "第二段。"],
        review=lambda request: True, synthesize=synthesize,
    )
    assert report["scope"] == {"sections": ["1.2"]}
    assert [(item["section_id"], item["status"]) for item in report["sections"]] == [("1.2", "success")]
    assert studio.published


def test_rerun_verifies_completed_section_without_regenerating_or_republishing(tmp_path):
    class CountingStudio(Studio):
        def __init__(self):
            super().__init__()
            self.uploads = self.publications = 0

        def upload_audio(self, locator, audio):
            self.uploads += 1
            return super().upload_audio(locator, audio)

        def publish_vertical(self, locator):
            self.publications += 1
            return super().publish_vertical(locator)

    package = tmp_path / "package"
    package_at(package)
    studio = CountingStudio()
    calls = []
    def split(request):
        calls.append("split")
        return ["# 测试", "第一段。", "第二段。"]
    def tts(*args):
        calls.append("tts")
        return synthesize(*args)
    options = dict(sections=["1.2"], studio=studio, split=split,
                   review=lambda request: True, synthesize=tts)
    first = run_batch(package, TARGET, package / "repair", **options)
    second = run_batch(package, TARGET, package / "repair", **options)
    assert first["sections"][0]["status"] == second["sections"][0]["status"] == "success"
    assert calls == ["split", "tts"]
    assert (studio.uploads, studio.publications) == (1, 1)


def test_explicit_repagination_rechecks_completed_target_and_forwards_feedback(tmp_path):
    class CountingStudio(Studio):
        def __init__(self):
            super().__init__()
            self.uploads = self.publications = 0

        def upload_audio(self, locator, path):
            self.uploads += 1
            return {"document_id": f"new-{self.uploads}.mp3", "file_name": f"new-{self.uploads}.mp3"}

        def publish_vertical(self, locator):
            self.publications += 1
            return super().publish_vertical(locator)

    package = tmp_path / "package"
    package_at(package)
    studio = CountingStudio()
    requests = []
    reviewed = []
    def split(request):
        requests.append(request)
        return ["# 测试", "第一段。", "第二段。"]
    def review(request):
        reviewed.append(request)
        return True
    options = dict(sections=["1.2"], studio=studio, split=split,
                   review=review, synthesize=synthesize)

    first = run_batch(package, TARGET, package / "repair", **options)
    old_state = json.loads((package / "repair" / "1.2" / "run-state.json").read_text(encoding="utf-8"))
    old_manifest = (package / "repair" / "1.2" / "imagesgallery.json").read_bytes()
    old_audio = (package / "repair" / "1.2" / "audio" / "full_speech.mp3").read_bytes()
    second = run_batch(package, TARGET, package / "repair", repaginate=True,
                       pagination_feedback="让标题与所属内容在同一页", **options)
    assert first["status"] == second["status"] == "success"
    assert (studio.uploads, studio.publications) == (2, 2)
    assert requests[1]["previous_pages"] == ["# 测试", "第一段。", "第二段。"]
    assert requests[1]["baseline_pages"] == ["# 测试", "第一段。", "第二段。"]
    assert requests[1]["user_feedback"] == "让标题与所属内容在同一页"
    assert reviewed[1]["user_feedback"] == "让标题与所属内容在同一页"
    state = json.loads((package / "repair" / "1.2" / "run-state.json").read_text(encoding="utf-8"))
    assert "revisions" in state["manifest_path"]
    assert (package / "repair" / "1.2" / "imagesgallery.json").read_bytes() == old_manifest
    assert (package / "repair" / "1.2" / "audio" / "full_speech.mp3").read_bytes() == old_audio
    revision = Path(state["manifest_path"]).parent
    assert json.loads((revision / "previous-state.json").read_text(encoding="utf-8")) == old_state
    third = run_batch(package, TARGET, package / "repair", **options)
    assert third["sections"][0]["reused"] and len(requests) == 2
    assert (studio.uploads, studio.publications) == (2, 2)


def test_repagination_synthesis_failure_preserves_completed_artifacts(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    studio = Studio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True)
    first = run_batch(package, TARGET, package / "repair", synthesize=synthesize, **options)
    state_path = package / "repair" / "1.2" / "run-state.json"
    state_before = state_path.read_bytes()
    manifest_before = (state_path.parent / "imagesgallery.json").read_bytes()
    audio_before = (state_path.parent / "audio" / "full_speech.mp3").read_bytes()
    def fail_synthesis(*args):
        raise RuntimeError("simulated failure")

    second = run_batch(package, TARGET, package / "repair", repaginate=True, synthesize=fail_synthesis, **options)
    assert first["status"] == "success" and second["status"] == "failed"
    assert state_path.read_bytes() == state_before
    assert (state_path.parent / "imagesgallery.json").read_bytes() == manifest_before
    assert (state_path.parent / "audio" / "full_speech.mp3").read_bytes() == audio_before
    assert len(list((state_path.parent / "revisions").glob("*/previous-state.json"))) == 1


def test_interrupted_repagination_resumes_from_its_revision_artifacts(tmp_path):
    class InterruptedStudio(Studio):
        def __init__(self):
            super().__init__()
            self.uploads = 0
            self.interrupt = False

        def upload_audio(self, locator, path):
            self.uploads += 1
            return {"document_id": f"new-{self.uploads}.mp3", "file_name": f"new-{self.uploads}.mp3"}

        def bind_audio(self, locator, binding):
            super().bind_audio(locator, binding)
            if self.interrupt:
                self.interrupt = False
                raise SystemExit("simulated interruption")

    package = tmp_path / "package"
    package_at(package)
    studio = InterruptedStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    assert run_batch(package, TARGET, package / "repair", **options)["status"] == "success"
    studio.interrupt = True
    with pytest.raises(SystemExit):
        run_batch(package, TARGET, package / "repair", repaginate=True, **options)
    state = json.loads((package / "repair" / "1.2" / "run-state.json").read_text(encoding="utf-8"))
    assert state["stage"] == "audio_uploaded" and "revisions" in state["manifest_path"]
    resumed = run_batch(package, TARGET, package / "repair", **options)
    assert resumed["status"] == "success" and studio.uploads == 2


def test_repagination_stops_if_completed_target_changed(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    studio = Studio()
    calls = []
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: calls.append("split") or ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    assert run_batch(package, TARGET, package / "repair", **options)["status"] == "success"
    studio.config["unrelated_setting"] = "concurrent edit"
    result = run_batch(package, TARGET, package / "repair", repaginate=True, **options)
    assert result["status"] == "unconfirmed" and calls == ["split"]


def test_pagination_feedback_requires_one_explicit_section(tmp_path):
    with pytest.raises(ValueError, match="one explicitly selected section"):
        run_batch(tmp_path, TARGET, tmp_path / "repair", studio=Studio(), synthesize=synthesize,
                  first_chapter=True, repaginate=True, pagination_feedback="feedback")


def test_replacement_resumes_after_delete_response_and_readback_are_lost(tmp_path):
    class InterruptedStudio(ReplacementStudio):
        def __init__(self):
            super().__init__()
            self.lost = False
            self.creates = 0
        def create_gallery(self, vertical):
            self.creates += 1
            return super().create_gallery(vertical)
        def delete_gallery(self, identity):
            super().delete_gallery(identity)
            self.lost = True
            raise TimeoutError("delete response lost")
        def get_course_structure(self, course_id):
            if self.lost:
                raise TimeoutError("readback unavailable")
            return super().get_course_structure(course_id)

    package = tmp_path / "package"
    package_at(package)
    studio = InterruptedStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    first = run_batch(package, TARGET, package / "repair", **options)
    assert first["sections"][0]["status"] == "unconfirmed"
    assert studio.deleted == [GALLERY] and not studio.published
    studio.lost = False
    second = run_batch(package, TARGET, package / "repair", **options)
    assert second["sections"][0]["status"] == "success"
    assert studio.creates == 1 and studio.deleted == [GALLERY] and studio.published
    assert ("imagesgallery", NEW_GALLERY) in studio.children


def test_in_place_interruption_after_audio_binding_resumes_without_reupload(tmp_path):
    class InterruptedStudio(Studio):
        def __init__(self):
            super().__init__()
            self.uploads = 0
            self.interrupt = True
        def upload_audio(self, locator, path):
            self.uploads += 1
            return super().upload_audio(locator, path)
        def bind_audio(self, locator, binding):
            super().bind_audio(locator, binding)
            if self.interrupt:
                self.interrupt = False
                raise SystemExit("process stopped")

    package = tmp_path / "package"
    package_at(package)
    studio = InterruptedStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    with pytest.raises(SystemExit):
        run_batch(package, TARGET, package / "repair", **options)
    result = run_batch(package, TARGET, package / "repair", **options)
    assert result["sections"][0]["status"] == "success"
    assert studio.uploads == 1 and studio.published


def test_in_place_publication_response_interruption_is_confirmed_on_rerun(tmp_path):
    class InterruptedStudio(Studio):
        def __init__(self):
            super().__init__()
            self.publications = 0
        def publish_vertical(self, locator):
            self.publications += 1
            super().publish_vertical(locator)
            raise SystemExit("process stopped")

    package = tmp_path / "package"
    package_at(package)
    studio = InterruptedStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    with pytest.raises(SystemExit):
        run_batch(package, TARGET, package / "repair", **options)
    result = run_batch(package, TARGET, package / "repair", **options)
    assert result["sections"][0]["status"] == "success"
    assert studio.publications == 1


def test_legacy_sequential_checkpoint_is_not_resumed_with_unit_scope(tmp_path):
    class InterruptedStudio(Studio):
        def __init__(self):
            super().__init__()
            self.publications = 0

        def publish_vertical(self, locator):
            self.publications += 1
            raise SystemExit("interrupted before publication")

    package = tmp_path / "package"
    package_at(package)
    studio = InterruptedStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    with pytest.raises(SystemExit):
        run_batch(package, TARGET, package / "repair", **options)
    state_path = package / "repair" / "1.2" / "run-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.pop("publication_scope")
    state_path.write_text(json.dumps(state), encoding="utf-8")

    report = run_batch(package, TARGET, package / "repair", **options)
    assert report["sections"][0]["status"] == "unconfirmed"
    assert "earlier publication scope" in report["sections"][0]["reason"]
    assert studio.publications == 1


def test_resume_rechecks_gallery_before_retrying_publication(tmp_path):
    class ConcurrentStudio(Studio):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def publish_vertical(self, locator):
            self.attempts += 1
            if self.attempts == 1:
                raise SystemExit("interrupted before publication")
            if self.attempts == 2:
                self.config["unrelated_setting"] = "concurrent edit"
                raise TimeoutError("response lost")
            return super().publish_vertical(locator)

    package = tmp_path / "package"
    package_at(package)
    studio = ConcurrentStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    with pytest.raises(SystemExit):
        run_batch(package, TARGET, package / "repair", **options)
    result = run_batch(package, TARGET, package / "repair", **options)
    assert result["sections"][0]["status"] == "unconfirmed"
    assert not studio.published and studio.attempts == 2


def test_failed_subtitle_update_during_resume_restores_old_binding(tmp_path):
    class InterruptedStudio(Studio):
        def __init__(self):
            super().__init__()
            self.interrupt = True
            self.old_config = copy.deepcopy(self.config)
        def bind_audio(self, locator, binding):
            super().bind_audio(locator, binding)
            if self.interrupt:
                self.interrupt = False
                raise SystemExit("process stopped")
        def update_play_config(self, locator, value):
            if value != self.old_config:
                raise TimeoutError("subtitle update failed")
            super().update_play_config(locator, value)

    package = tmp_path / "package"
    package_at(package)
    studio = InterruptedStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    with pytest.raises(SystemExit):
        run_batch(package, TARGET, package / "repair", **options)
    result = run_batch(package, TARGET, package / "repair", **options)["sections"][0]
    assert result["status"] == "failed" and result["recovery"] == "restored and verified"
    assert studio.audios == [{"document_id": "old.mp3", "file_name": "old.mp3"}]
    assert studio.config == studio.old_config and not studio.published


def test_replacement_resumes_after_image_upload_without_creating_again(tmp_path):
    class InterruptedStudio(ReplacementStudio):
        def __init__(self):
            super().__init__()
            self.creates = 0
            self.interrupt = True
        def create_gallery(self, vertical):
            self.creates += 1
            return super().create_gallery(vertical)
        def upload_image(self, locator, path):
            result = super().upload_image(locator, path)
            if self.interrupt:
                self.interrupt = False
                raise SystemExit("process stopped")
            return result

    package = tmp_path / "package"
    package_at(package)
    studio = InterruptedStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    with pytest.raises(SystemExit):
        run_batch(package, TARGET, package / "repair", **options)
    result = run_batch(package, TARGET, package / "repair", **options)
    assert result["sections"][0]["status"] == "success"
    assert studio.creates == 1 and len(studio.uploaded_paths) == 3 and studio.published


def test_resume_preserves_concurrent_new_gallery_config(tmp_path):
    class InterruptedStudio(ReplacementStudio):
        interrupt = True

        def upload_image(self, locator, path):
            result = super().upload_image(locator, path)
            if self.interrupt:
                self.interrupt = False
                raise SystemExit("interrupted after image upload")
            return result

    package = tmp_path / "package"
    package_at(package)
    studio = InterruptedStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    with pytest.raises(SystemExit):
        run_batch(package, TARGET, package / "repair", **options)
    studio.configs[NEW_GALLERY] = {"concurrent_setting": "keep"}
    report = run_batch(package, TARGET, package / "repair", **options)
    assert report["sections"][0]["status"] == "unconfirmed"
    assert studio.configs[NEW_GALLERY] == {"concurrent_setting": "keep"}
    assert studio.deleted == [] and not studio.published


def test_replacement_without_confirmed_creation_identity_stays_unconfirmed(tmp_path):
    class InterruptedStudio(ReplacementStudio):
        def __init__(self):
            super().__init__()
            self.creates = 0
        def create_gallery(self, vertical):
            self.creates += 1
            super().create_gallery(vertical)
            raise SystemExit("process stopped")

    package = tmp_path / "package"
    package_at(package)
    studio = InterruptedStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    with pytest.raises(SystemExit):
        run_batch(package, TARGET, package / "repair", **options)
    result = run_batch(package, TARGET, package / "repair", **options)
    assert result["sections"][0]["status"] == "unconfirmed"
    assert studio.creates == 1 and studio.deleted == [] and not studio.published


def test_replacement_resumes_confirmed_empty_gallery_before_initial_config_saved(tmp_path):
    class InterruptedStudio(ReplacementStudio):
        def __init__(self):
            super().__init__()
            self.interrupt = False
            self.creates = 0

        def create_gallery(self, vertical):
            self.creates += 1
            identity = super().create_gallery(vertical)
            self.interrupt = True
            return identity

        def get_course_structure(self, course_id):
            if self.interrupt:
                self.interrupt = False
                raise SystemExit("interrupted after confirmed creation")
            return super().get_course_structure(course_id)

    package = tmp_path / "package"
    package_at(package)
    studio = InterruptedStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    with pytest.raises(SystemExit):
        run_batch(package, TARGET, package / "repair", **options)
    report = run_batch(package, TARGET, package / "repair", **options)
    assert report["sections"][0]["status"] == "success"
    assert studio.creates == 1 and studio.deleted == [GALLERY] and studio.published


def test_replacement_does_not_resume_if_confirmed_gallery_was_edited(tmp_path):
    class InterruptedStudio(ReplacementStudio):
        interrupt = False

        def create_gallery(self, vertical):
            identity = super().create_gallery(vertical)
            self.interrupt = True
            return identity

        def get_course_structure(self, course_id):
            if self.interrupt:
                self.interrupt = False
                raise SystemExit("interrupted after confirmed creation")
            return super().get_course_structure(course_id)

    package = tmp_path / "package"
    package_at(package)
    studio = InterruptedStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    with pytest.raises(SystemExit):
        run_batch(package, TARGET, package / "repair", **options)
    studio.configs[NEW_GALLERY] = {"concurrent_setting": "keep"}
    result = run_batch(package, TARGET, package / "repair", **options)
    assert result["sections"][0]["status"] == "unconfirmed"
    assert studio.configs[NEW_GALLERY] == {"concurrent_setting": "keep"}
    assert studio.uploaded_paths == [] and studio.deleted == [] and not studio.published


def test_replacement_resumes_after_new_audio_binding_without_reupload(tmp_path):
    class InterruptedStudio(ReplacementStudio):
        def __init__(self):
            super().__init__()
            self.audio_uploads = 0
            self.interrupt = True
        def upload_audio(self, locator, path):
            self.audio_uploads += 1
            return super().upload_audio(locator, path)
        def bind_audio(self, locator, binding):
            super().bind_audio(locator, binding)
            if self.interrupt:
                self.interrupt = False
                raise SystemExit("process stopped")

    package = tmp_path / "package"
    package_at(package)
    studio = InterruptedStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    with pytest.raises(SystemExit):
        run_batch(package, TARGET, package / "repair", **options)
    result = run_batch(package, TARGET, package / "repair", **options)
    assert result["sections"][0]["status"] == "success"
    assert studio.audio_uploads == 1 and studio.deleted == [GALLERY] and studio.published


def test_replacement_resumes_after_reorder_without_recreating_gallery(tmp_path):
    class InterruptedStudio(ReplacementStudio):
        def __init__(self):
            super().__init__()
            self.creates = 0
            self.interrupt = True
        def create_gallery(self, vertical):
            self.creates += 1
            return super().create_gallery(vertical)
        def reorder_children(self, vertical, identities):
            super().reorder_children(vertical, identities)
            if self.interrupt:
                self.interrupt = False
                raise SystemExit("process stopped")

    package = tmp_path / "package"
    package_at(package)
    studio = InterruptedStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    with pytest.raises(SystemExit):
        run_batch(package, TARGET, package / "repair", **options)
    result = run_batch(package, TARGET, package / "repair", **options)
    assert result["sections"][0]["status"] == "success"
    assert studio.creates == 1 and studio.deleted == [GALLERY] and studio.published


def test_changed_input_and_changed_online_gallery_are_not_overwritten(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    studio = Studio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    assert run_batch(package, TARGET, package / "repair", **options)["sections"][0]["status"] == "success"
    source = package / "course-content.json"
    content = json.loads(source.read_text(encoding="utf-8"))
    content["chapters"][0]["sections"][0]["verticals"][0]["blocks"][0]["text"] += "<p>新内容。</p>"
    source.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    studio.config["unrelated_setting"] = "someone else changed this"
    result = run_batch(package, TARGET, package / "repair", **options)
    assert result["sections"][0]["status"] == "unconfirmed"
    assert studio.config["unrelated_setting"] == "someone else changed this"


def test_first_chapter_scope_uses_course_structure_and_does_not_expand(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    manifest = package / "section-list.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["sections"].append({"section_id": "2.1", "section_title": "第二章测试", "block_type": "html"})
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    source = package / "course-content.json"
    content = json.loads(source.read_text(encoding="utf-8"))
    other = json.loads(json.dumps(content["chapters"][0], ensure_ascii=False))
    other["name"] = "第二章"
    other["sections"][0]["name"] = "第二章测试"
    content["chapters"].append(other)
    content["chapters"].insert(0, {"name": "训战启程", "sections": [{"name": "训战启程", "verticals": []}]})
    source.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    studio = Studio()
    report = run_batch(package, TARGET, package / "repair", first_chapter=True, studio=studio,
                       split=lambda request: ["# 测试", "第一段。", "第二段。"],
                       review=lambda request: True, synthesize=synthesize)
    assert report["scope"] == {"first_chapter": "第一章"}
    assert [(item["section_id"], item["status"]) for item in report["sections"]] == [("1.2", "success")]


def test_invalid_section_does_not_block_other_selected_sections(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    studio = Studio()
    report = run_batch(package, TARGET, package / "repair", sections=["1.99", "1.2"], studio=studio,
                       split=lambda request: ["# 测试", "第一段。", "第二段。"],
                       review=lambda request: True, synthesize=synthesize)
    assert [(item["section_id"], item["status"]) for item in report["sections"]] == [("1.99", "failed"), ("1.2", "success")]
    assert studio.published and report["status"] == "failed"


def test_unconfirmed_in_place_audio_upload_is_read_back_without_retry(tmp_path):
    class LostUploadStudio(Studio):
        def __init__(self):
            super().__init__()
            self.uploads = self.readbacks = 0
        def upload_audio(self, locator, path):
            self.uploads += 1
            raise TimeoutError("registration response lost")
        def get_audios(self, locator):
            if self.uploads:
                self.readbacks += 1
            return super().get_audios(locator)

    package = tmp_path / "package"
    package_at(package)
    studio = LostUploadStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    assert run_batch(package, TARGET, package / "repair", **options)["sections"][0]["status"] == "unconfirmed"
    assert run_batch(package, TARGET, package / "repair", **options)["sections"][0]["status"] == "unconfirmed"
    assert studio.uploads == 1 and studio.readbacks >= 2 and not studio.published


def test_completed_replacement_is_not_recreated_on_rerun(tmp_path):
    class CountingReplacementStudio(ReplacementStudio):
        def __init__(self):
            super().__init__()
            self.creates = 0
        def create_gallery(self, vertical):
            self.creates += 1
            return super().create_gallery(vertical)

    package = tmp_path / "package"
    package_at(package)
    studio = CountingReplacementStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    assert run_batch(package, TARGET, package / "repair", **options)["sections"][0]["status"] == "success"
    result = run_batch(package, TARGET, package / "repair", **options)
    assert result["sections"][0]["status"] == "success"
    assert studio.creates == 1 and len(studio.uploaded_paths) == 3 and studio.deleted == [GALLERY]


def test_completed_replacement_with_changed_isolated_artifact_is_not_reused(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    studio = ReplacementStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    assert run_batch(package, TARGET, package / "repair", **options)["sections"][0]["status"] == "success"
    studio.uploaded_paths[0].write_bytes(b"changed")
    result = run_batch(package, TARGET, package / "repair", **options)
    assert result["sections"][0]["status"] == "unconfirmed"
    assert len(studio.uploaded_paths) == 3 and studio.deleted == [GALLERY]


def test_completed_replacement_with_changed_metadata_is_not_reused(tmp_path):
    class ChangedStudio(ReplacementStudio):
        changed = False

        def get_block_details(self, locator):
            details = super().get_block_details(locator)
            if locator == NEW_GALLERY and self.changed:
                details["metadata"]["content_names"] = ["concurrent-change.jpg"]
            return details

    package = tmp_path / "package"
    package_at(package)
    studio = ChangedStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    assert run_batch(package, TARGET, package / "repair", **options)["sections"][0]["status"] == "success"
    studio.changed = True
    result = run_batch(package, TARGET, package / "repair", **options)
    assert result["sections"][0]["status"] == "unconfirmed"
    assert len(studio.uploaded_paths) == 3 and studio.deleted == [GALLERY]


def test_gallery_credentials_are_not_written_to_run_state(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    studio = Studio()
    studio.config["access_token"] = "private-credential"
    report = run_batch(package, TARGET, package / "repair", sections=["1.2"], studio=studio,
                       split=lambda request: ["# 测试", "第一段。", "第二段。"],
                       review=lambda request: True, synthesize=synthesize)
    assert report["sections"][0]["status"] == "failed" and not studio.published
    assert all("private-credential" not in path.read_text(encoding="utf-8")
               for path in (package / "repair").rglob("*.json"))


def test_corrupted_run_state_stops_before_new_studio_writes(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    studio = Studio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    assert run_batch(package, TARGET, package / "repair", **options)["sections"][0]["status"] == "success"
    audio = copy.deepcopy(studio.audios)
    (package / "repair" / "1.2" / "run-state.json").write_text("{}", encoding="utf-8")
    result = run_batch(package, TARGET, package / "repair", **options)
    assert result["sections"][0]["status"] == "unconfirmed"
    assert studio.audios == audio


def test_changed_old_pagination_reuses_verified_isolated_images(tmp_path):
    class CountingReplacementStudio(ReplacementStudio):
        def __init__(self):
            super().__init__()
            self.creates = self.audio_uploads = 0
        def create_gallery(self, vertical):
            self.creates += 1
            return super().create_gallery(vertical)
        def upload_audio(self, locator, audio):
            self.audio_uploads += 1
            return {"document_id": f"new-{self.audio_uploads}.mp3", "file_name": f"new-{self.audio_uploads}.mp3"}

    package = tmp_path / "package"
    package_at(package)
    studio = CountingReplacementStudio()
    options = dict(sections=["1.2"], studio=studio,
                   split=lambda request: ["# 测试", "第一段。", "第二段。"],
                   review=lambda request: True, synthesize=synthesize)
    assert run_batch(package, TARGET, package / "repair", **options)["sections"][0]["status"] == "success"
    manifest = package / "section-list.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["sections"][0]["page_content"][1] = "变更的旧分页线索"
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = run_batch(package, TARGET, package / "repair", **options)
    assert result["sections"][0]["status"] == "success"
    assert studio.creates == 1 and studio.audio_uploads == 2 and studio.deleted == [GALLERY]
    assert result["sections"][0]["image_match"] == "isolated local uploads"
    assert (package / "repair" / "1.2" / "image-map.json").is_file()


def test_batch_keeps_agent_requests_isolated_and_serial(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    source_path = package / "course-content.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    second = copy.deepcopy(source["chapters"][0]["sections"][0])
    second["name"] = "1.3 另一节"
    second["verticals"][0]["blocks"][0]["text"] = "<h1>另一节</h1><p>甲文。</p><p>乙文。</p>"
    source["chapters"][0]["sections"].append(second)
    source_path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    manifest_path = package / "section-list.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sections"].append({"section_id": "1.3", "section_title": "1.3 另一节", "block_type": "html", "page_content": ["旧另一节"]})
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    old_gallery = package / "1.2 测试" / "imagesgallery"
    second_gallery = package / "1.3 另一节" / "imagesgallery"
    shutil.copytree(old_gallery, second_gallery)
    gallery_path = second_gallery / "imagesgallery.json"
    gallery = json.loads(gallery_path.read_text(encoding="utf-8"))
    gallery.update({"section_id": "1.3", "section_title": "1.3 另一节"})
    gallery_path.write_text(json.dumps(gallery, ensure_ascii=False), encoding="utf-8")
    second_sequential = SEQUENTIAL + "2"
    second_vertical = VERTICAL + "2"
    second_gallery_id = OLD_GALLERY + "2"

    class MultiStudio:
        def __init__(self):
            self.first, self.second = Studio(), Studio()
        def get_course_structure(self, course_id):
            tree = self.first.get_course_structure(course_id)
            chapter = tree["child_info"]["children"][0]
            new_section = copy.deepcopy(chapter["child_info"]["children"][0])
            new_section.update({"id": second_sequential, "display_name": "1.3 另一节"})
            unit = new_section["child_info"]["children"][0]
            unit["id"] = second_vertical
            unit["child_info"]["children"][0]["id"] = second_gallery_id
            chapter["child_info"]["children"].append(new_section)
            return tree
        def select(self, locator):
            return self.second if locator in (second_gallery_id, second_sequential, second_vertical) else self.first
        def get_block_details(self, locator): return self.first.get_block_details(locator)
        def get_files(self, locator): return self.select(locator).get_files(locator)
        def get_audios(self, locator): return self.select(locator).get_audios(locator)
        def get_play_config(self, locator): return self.select(locator).get_play_config(locator)
        def upload_audio(self, locator, path): return self.select(locator).upload_audio(locator, path)
        def bind_audio(self, locator, value): return self.select(locator).bind_audio(locator, value)
        def update_play_config(self, locator, value): return self.select(locator).update_play_config(locator, value)
        def publish_vertical(self, locator): return self.select(locator).publish_vertical(locator)
        def get_course_publication_structure(self, course_id):
            return {SEQUENTIAL: {"published": True, "has_changes": not self.first.published, "child_info": {"children": [
                        {"category": "vertical", "id": VERTICAL, "published": self.first.published, "has_changes": not self.first.published}]}},
                    second_sequential: {"published": True, "has_changes": not self.second.published, "child_info": {"children": [
                        {"category": "vertical", "id": second_vertical, "published": self.second.published, "has_changes": not self.second.published}]}}}

    active = 0
    maximum = 0
    requests = []
    def agent(kind, request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        requests.append((kind, request["source"]))
        active -= 1
        if kind == "review":
            return True
        return ["# 另一节", "甲文。", "乙文。"] if "甲文" in request["source"] else ["# 测试", "第一段。", "第二段。"]
    def tts(manifest, voice, output):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        output.mkdir(parents=True, exist_ok=True)
        audio = output / "full_speech.mp3"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "3", str(audio)], check=True)
        for index, item in enumerate(data["items"]):
            item["subtitle"] = item.pop("speech")
            item["start"], item["end"] = index * 1000, (index + 1) * 1000
        data["audio"] = {**voice, "full_audio": str(audio)}
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return audio

    report = run_batch(package, TARGET, package / "repair", chapter="第一章", studio=MultiStudio(),
                       split=lambda request: agent("split", request),
                       review=lambda request: agent("review", request), synthesize=tts)
    assert [row["status"] for row in report["sections"]] == ["success", "success"]
    assert maximum == 1 and [kind for kind, _ in requests] == ["split", "review", "split", "review"]
    assert "甲文" not in requests[0][1] and "第一段" not in requests[2][1]
