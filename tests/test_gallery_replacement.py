import copy
import json
import sys
import types
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests

from scripts.production import Studio as RealStudio
from scripts.repair_one import repair_one
from test_repair_one import GALLERY, SEQUENTIAL, Studio, SUMMARY, VERTICAL, package_at, synthesize, write_json
from test_production_adapter import Bucket, FakeSession, Response


NEW_GALLERY = "block-v1:FIRAx+TARGET+20261113+type@imagesgallery+block@new"
BEFORE = "block-v1:FIRAx+TARGET+20261113+type@html+block@before"
AFTER = "block-v1:FIRAx+TARGET+20261113+type@problem+block@after"
TARGET = "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113"


class ReplacementStudio(Studio):
    def __init__(self):
        super().__init__()
        self.children = [("html", BEFORE), ("imagesgallery", GALLERY), ("html", SUMMARY), ("problem", AFTER)]
        self.files = {GALLERY: [{"id": f"old-{n}", "filename": f"{n}.jpg"} for n in (2, 1, 3)]}
        self.configs = {GALLERY: copy.deepcopy(self.config)}
        self.audio_bindings = {GALLERY: copy.deepcopy(self.audios)}
        self.uploaded_paths = []
        self.deleted = []

    def get_course_structure(self, course_id):
        tree = super().get_course_structure(course_id)
        vertical = tree["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]
        vertical["child_info"]["children"] = [{"category": kind, "id": identity, **({"has_children": False} if kind == "imagesgallery" else {})} for kind, identity in self.children]
        return tree

    def get_files(self, locator):
        rows = self.files[locator]
        return {"files": copy.deepcopy(rows), "total_count": len(rows)}

    def get_block_details(self, locator):
        details = super().get_block_details(locator)
        if locator == NEW_GALLERY and locator in self.files:
            metadata = {}
            if self.files[locator]:
                metadata["contents"] = copy.deepcopy(self.files[locator])
                metadata["content_names"] = [row.get("display_name") or row.get("filename") for row in self.files[locator]]
            if self.audio_bindings[locator]:
                metadata["audio_contents"] = copy.deepcopy(self.audio_bindings[locator])
            if self.configs[locator]:
                metadata["play_config"] = copy.deepcopy(self.configs[locator])
            details["metadata"] = metadata
        return details

    def get_play_config(self, locator):
        return {"play_config": copy.deepcopy(self.configs[locator])}

    def get_audios(self, locator):
        return {"audios": copy.deepcopy(self.audio_bindings[locator])}

    def create_gallery(self, vertical):
        assert vertical == VERTICAL
        self.children.append(("imagesgallery", NEW_GALLERY))
        self.files[NEW_GALLERY] = []
        self.configs[NEW_GALLERY] = {}
        self.audio_bindings[NEW_GALLERY] = []
        return NEW_GALLERY

    def upload_image(self, locator, path):
        path = Path(path)
        self.uploaded_paths.append(path)
        identity = f"new-{len(self.uploaded_paths)}"
        self.files[locator].append({"id": identity, "asset_key": f"asset:{identity}", "display_name": path.name})
        return identity

    def order_images(self, locator, ids):
        by_id = {item["id"]: item for item in self.files[locator]}
        self.files[locator] = [by_id[identity] for identity in ids]

    def bind_audio(self, locator, binding):
        self.audio_bindings[locator] = [copy.deepcopy(binding)]

    def update_play_config(self, locator, config):
        self.configs[locator] = copy.deepcopy(config)

    def reorder_children(self, vertical, identities):
        assert vertical == VERTICAL
        categories = {identity: kind for kind, identity in self.children}
        assert set(identities) == set(categories)
        self.children = [(categories[identity], identity) for identity in identities]

    def delete_gallery(self, identity):
        assert identity == GALLERY
        assert len(self.files[NEW_GALLERY]) == 3
        assert len(self.audio_bindings[NEW_GALLERY]) == 1
        assert len(self.configs[NEW_GALLERY]["time_config"]) == 3
        self.children = [item for item in self.children if item[1] != identity]
        self.deleted.append(identity)


def run(package, studio, **options):
    return repair_one(package, TARGET, "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=synthesize, studio=studio, **options)


def test_different_image_order_replaces_only_gallery_after_complete_readback(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    before = {p.relative_to(package): p.read_bytes() for p in package.rglob("*") if p.is_file()}
    studio = ReplacementStudio()
    result = run(package, studio)

    assert result["status"] == "success"
    assert studio.children == [("html", BEFORE), ("imagesgallery", NEW_GALLERY), ("html", SUMMARY), ("problem", AFTER)]
    assert studio.deleted == [GALLERY] and studio.published
    assert len(studio.audio_bindings[NEW_GALLERY]) == 1
    assert studio.configs[NEW_GALLERY]["unrelated_setting"] == "keep"
    assert [item["subtitle"] for item in studio.configs[NEW_GALLERY]["time_config"]] == ["# 测试", "第一段。", "第二段。"]
    assert all(path.is_relative_to(package / "repair") and path.name not in {"1.jpg", "2.jpg", "3.jpg"} for path in studio.uploaded_paths)
    mapping = json.loads((package / "repair" / "1.2" / "image-map.json").read_text(encoding="utf-8"))
    assert len(mapping) == 3 and [item["asset_id"] for item in mapping] == ["new-1", "new-2", "new-3"]
    assert all((package / path).read_bytes() == data for path, data in before.items())


def test_new_gallery_with_unreadable_children_never_replaces_old_gallery(tmp_path):
    class IncompleteCandidate(ReplacementStudio):
        def get_course_structure(self, course_id):
            tree = super().get_course_structure(course_id)
            unit = tree["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]
            for child in unit["child_info"]["children"]:
                if child.get("id") == NEW_GALLERY:
                    child["has_children"] = True
            return tree

    package = tmp_path / "package"
    package_at(package)
    studio = IncompleteCandidate()
    result = run(package, studio)
    assert result["status"] == "unconfirmed"
    assert studio.uploaded_paths == [] and studio.deleted == [] and not studio.published


def test_uncertain_creation_never_repeats_or_publishes(tmp_path):
    class TimedOutCreation(ReplacementStudio):
        def __init__(self):
            super().__init__()
            self.creates = 0

        def create_gallery(self, vertical):
            self.creates += 1
            raise TimeoutError("no creation outcome")

    package = tmp_path / "package"
    package_at(package)
    studio = TimedOutCreation()
    assert run(package, studio)["status"] == "unconfirmed"
    assert run(package, studio)["status"] == "unconfirmed"
    assert studio.creates == 1 and not studio.published and studio.deleted == []
    assert ("imagesgallery", GALLERY) in studio.children


def test_unattributed_new_gallery_is_never_claimed_after_timeout(tmp_path):
    class ConcurrentCreation(ReplacementStudio):
        def create_gallery(self, vertical):
            # Another editor creates this block while our own request fails.
            super().create_gallery(vertical)
            raise TimeoutError("our create outcome is unknown")

    package = tmp_path / "package"
    package_at(package)
    studio = ConcurrentCreation()
    assert run(package, studio)["status"] == "unconfirmed"
    assert run(package, studio)["status"] == "unconfirmed"
    assert studio.uploaded_paths == [] and studio.deleted == [] and not studio.published


def test_concurrent_new_gallery_config_is_not_overwritten(tmp_path):
    class ConcurrentConfig(ReplacementStudio):
        def upload_image(self, locator, path):
            identity = super().upload_image(locator, path)
            self.configs[locator] = {"concurrent_setting": "keep"}
            return identity

    package = tmp_path / "package"
    package_at(package)
    studio = ConcurrentConfig()
    assert run(package, studio)["status"] == "unconfirmed"
    assert studio.configs[NEW_GALLERY] == {"concurrent_setting": "keep"}
    assert studio.deleted == [] and not studio.published


def test_nonempty_new_gallery_initial_config_is_not_overwritten(tmp_path):
    class ChangedAtCreation(ReplacementStudio):
        def create_gallery(self, vertical):
            identity = super().create_gallery(vertical)
            self.configs[identity] = {"concurrent_setting": "keep"}
            return identity

    package = tmp_path / "package"
    package_at(package)
    studio = ChangedAtCreation()
    assert run(package, studio)["status"] == "unconfirmed"
    assert studio.configs[NEW_GALLERY] == {"concurrent_setting": "keep"}
    assert studio.uploaded_paths == [] and studio.deleted == [] and not studio.published


def test_new_gallery_with_unexpected_metadata_is_not_claimed(tmp_path):
    class ChangedMetadata(ReplacementStudio):
        def get_block_details(self, locator):
            details = super().get_block_details(locator)
            if locator == NEW_GALLERY:
                details["metadata"] = {"display_name": "并发修改"}
            return details

    package = tmp_path / "package"
    package_at(package)
    studio = ChangedMetadata()
    assert run(package, studio)["status"] == "unconfirmed"
    assert studio.uploaded_paths == [] and studio.deleted == [] and not studio.published


def test_concurrent_new_gallery_metadata_change_stops_replacement(tmp_path):
    class ChangedMetadata(ReplacementStudio):
        edited = False

        def upload_image(self, locator, path):
            result = super().upload_image(locator, path)
            self.edited = True
            return result

        def get_block_details(self, locator):
            details = super().get_block_details(locator)
            if locator == NEW_GALLERY and self.edited:
                details["metadata"] = {"display_name": "并发修改"}
            return details

    package = tmp_path / "package"
    package_at(package)
    studio = ChangedMetadata()
    assert run(package, studio)["status"] == "unconfirmed"
    assert studio.deleted == [] and not studio.published


@pytest.mark.parametrize("field,value", [
    ("contents", [{"id": "other", "display_name": "other.jpg"}]),
    ("content_names", ["concurrent-change.jpg"]),
    ("audio_contents", [{"document_id": "other.mp3", "file_name": "other.mp3"}]),
    ("play_config", {"concurrent_setting": True}),
])
def test_concurrent_new_gallery_metadata_mismatch_stops_replacement(tmp_path, field, value):
    class ChangedMetadata(ReplacementStudio):
        def get_block_details(self, locator):
            details = super().get_block_details(locator)
            if locator == NEW_GALLERY and self.files[locator]:
                details["metadata"][field] = value
            return details

    package = tmp_path / "package"
    package_at(package)
    studio = ChangedMetadata()
    result = run(package, studio)
    assert result["status"] == "unconfirmed" and studio.deleted == [] and not studio.published


def test_consistent_but_unexpected_candidate_image_stops_before_audio_upload(tmp_path):
    class ConcurrentFile(ReplacementStudio):
        changed = False
        audio_uploads = 0

        def get_block_details(self, locator):
            if locator == NEW_GALLERY and len(self.files[locator]) == 3 and not self.changed:
                self.changed = True
                self.files[locator].append({"id": "other", "display_name": "other.jpg"})
            return super().get_block_details(locator)

        def upload_audio(self, locator, path):
            self.audio_uploads += 1
            return super().upload_audio(locator, path)

    package = tmp_path / "package"
    package_at(package)
    studio = ConcurrentFile()
    result = run(package, studio)
    assert result["status"] == "unconfirmed" and studio.audio_uploads == 0
    assert studio.deleted == [] and not studio.published


def test_candidate_metadata_change_after_delete_is_not_published(tmp_path):
    class ChangedBeforePublish(ReplacementStudio):
        after_delete_reads = 0

        def get_block_details(self, locator):
            details = super().get_block_details(locator)
            if locator == NEW_GALLERY and self.deleted:
                self.after_delete_reads += 1
                if self.after_delete_reads >= 2:
                    details["metadata"]["content_names"] = ["concurrent-change.jpg"]
            return details

    package = tmp_path / "package"
    package_at(package)
    studio = ChangedBeforePublish()
    result = run(package, studio)
    assert result["status"] == "unconfirmed"
    assert studio.deleted == [GALLERY] and not studio.published


def test_candidate_metadata_change_during_publication_is_not_reported_as_success(tmp_path):
    class ChangedDuringPublish(ReplacementStudio):
        def get_block_details(self, locator):
            details = super().get_block_details(locator)
            if locator == NEW_GALLERY and self.published:
                details["metadata"]["content_names"] = ["concurrent-change.jpg"]
            return details

    package = tmp_path / "package"
    package_at(package)
    studio = ChangedDuringPublish()
    result = run(package, studio)
    assert result["status"] == "unconfirmed" and studio.published


def test_timed_out_image_and_delete_are_reconciled_from_current_studio_state(tmp_path):
    class TimedOutWrites(ReplacementStudio):
        def __init__(self):
            super().__init__()
            self.creates = self.uploads = self.deletes = 0

        def create_gallery(self, vertical):
            self.creates += 1
            return super().create_gallery(vertical)

        def upload_image(self, locator, path):
            self.uploads += 1
            super().upload_image(locator, path)
            raise TimeoutError("uploaded but response lost")

        def delete_gallery(self, identity):
            self.deletes += 1
            super().delete_gallery(identity)
            raise TimeoutError("deleted but response lost")

    package = tmp_path / "package"
    package_at(package)
    studio = TimedOutWrites()
    result = run(package, studio)
    assert result["status"] == "success" and studio.published
    assert (studio.creates, studio.uploads, studio.deletes) == (1, 3, 1)


def test_unconfirmed_upload_stops_before_deleting_old_gallery(tmp_path):
    class TimedOutUpload(ReplacementStudio):
        def __init__(self):
            super().__init__()
            self.uploads = 0

        def upload_image(self, locator, path):
            self.uploads += 1
            raise TimeoutError("no upload outcome")

    package = tmp_path / "package"
    package_at(package)
    studio = TimedOutUpload()
    assert run(package, studio)["status"] == "unconfirmed"
    assert run(package, studio)["status"] == "unconfirmed"
    assert studio.uploads == 1 and studio.deleted == [] and not studio.published


def test_unconfirmed_audio_upload_is_read_back_and_not_repeated(tmp_path):
    class TimedOutAudio(ReplacementStudio):
        def __init__(self):
            super().__init__()
            self.uploads = 0
            self.readbacks = 0

        def upload_audio(self, locator, audio):
            self.uploads += 1
            raise TimeoutError("audio registration outcome unknown")

        def get_audios(self, locator):
            if self.uploads and locator == NEW_GALLERY:
                self.readbacks += 1
            return super().get_audios(locator)

    package = tmp_path / "package"
    package_at(package)
    studio = TimedOutAudio()
    result = run(package, studio)
    assert result["status"] == "unconfirmed" and studio.readbacks >= 1
    assert run(package, studio)["status"] == "unconfirmed"
    assert studio.uploads == 1 and studio.deleted == [] and not studio.published


def test_unreadable_after_delete_never_publishes(tmp_path):
    class LostReadback(ReplacementStudio):
        def __init__(self):
            super().__init__()
            self.lost = False

        def delete_gallery(self, identity):
            super().delete_gallery(identity)
            self.lost = True
            raise TimeoutError("response lost")

        def get_course_structure(self, course_id):
            if self.lost:
                raise TimeoutError("readback unavailable")
            return super().get_course_structure(course_id)

    package = tmp_path / "package"
    package_at(package)
    studio = LostReadback()
    result = run(package, studio)
    assert result["status"] == "unconfirmed"
    assert result["replacement_stage"] == "delete_pending" and result["candidate_gallery"] == NEW_GALLERY
    assert studio.deleted == [GALLERY] and not studio.published
    state = json.loads((package / "repair" / "1.2" / "replacement-state.json").read_text(encoding="utf-8"))
    assert state["stage"] == "delete_pending"


def test_wrong_image_count_and_reversed_uploads_end_in_local_order(tmp_path):
    class ReverseUploads(ReplacementStudio):
        def upload_image(self, locator, path):
            identity = super().upload_image(locator, path)
            self.files[locator].insert(0, self.files[locator].pop())
            return identity

    package = tmp_path / "package"
    package_at(package)
    studio = ReverseUploads()
    studio.files[GALLERY] = studio.files[GALLERY][:2]
    result = run(package, studio)
    assert result["status"] == "success" and studio.published
    assert [item["id"] for item in studio.files[NEW_GALLERY]] == ["new-1", "new-2", "new-3"]


def test_concurrent_unit_change_stops_before_old_gallery_is_deleted(tmp_path):
    class ConcurrentChange(ReplacementStudio):
        def upload_image(self, locator, path):
            identity = super().upload_image(locator, path)
            self.children.append(("html", "someone-else"))
            return identity

    package = tmp_path / "package"
    package_at(package)
    studio = ConcurrentChange()
    result = run(package, studio)
    assert result["status"] == "unconfirmed"
    assert ("html", "someone-else") in studio.children
    assert studio.deleted == [] and not studio.published


def test_bad_new_gallery_readback_preserves_old_gallery(tmp_path):
    class CorruptSubtitle(ReplacementStudio):
        def update_play_config(self, locator, config):
            super().update_play_config(locator, config)
            if locator == NEW_GALLERY:
                self.configs[locator]["time_config"][0]["subtitle"] = "different"

    package = tmp_path / "package"
    package_at(package)
    studio = CorruptSubtitle()
    result = run(package, studio)
    assert result["status"] == "unconfirmed"
    assert studio.deleted == [] and not studio.published
    assert ("imagesgallery", GALLERY) in studio.children


def test_lost_publication_response_is_confirmed_by_readback(tmp_path):
    class TimedOutPublish(ReplacementStudio):
        def __init__(self):
            super().__init__()
            self.publishes = 0

        def publish_vertical(self, locator):
            self.publishes += 1
            super().publish_vertical(locator)
            raise TimeoutError("published but response lost")

    package = tmp_path / "package"
    package_at(package)
    studio = TimedOutPublish()
    result = run(package, studio)
    assert result["status"] == "success"
    assert studio.publishes == 1 and studio.deleted == [GALLERY]


def test_known_content_difference_forces_replacement_even_when_names_match(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    studio = ReplacementStudio()
    studio.files[GALLERY] = [{"id": f"old-{n}", "filename": f"{n}.jpg"} for n in (1, 2, 3)]
    result = run(package, studio, force_replace=True)
    assert result["status"] == "success" and studio.deleted == [GALLERY] and studio.published


def test_existing_studio_adapter_replaces_gallery_through_simulated_http(tmp_path, monkeypatch):
    class ReplacementSession(FakeSession):
        def request(self, method, url, **kwargs):
            path = urlparse(url).path
            body = kwargs.get("json") or {}
            if path == "/xblock/" and method == "POST":
                return Response({"locator": self.state.create_gallery(body["parent_locator"])})
            if "/xblock/" in path and "/handler/" in path:
                locator = path.split("/xblock/", 1)[1].split("/", 1)[0]
                handler = path.rsplit("/", 1)[-1]
                if handler == "get_files":
                    return Response(self.state.get_files(locator))
                if handler == "get_play_config":
                    return Response(self.state.get_play_config(locator))
                if handler == "get_audios":
                    return Response(self.state.get_audios(locator))
                if handler == "file_upload":
                    name = kwargs["files"]["files"][0]
                    identity = self.state.upload_image(locator, name)
                    return Response([{"id": identity, "asset_key": f"asset:{identity}", "display_name": name}])
                if handler == "change_files_order":
                    self.state.order_images(locator, body["ids"])
                    return Response({})
                if handler == "save_audio":
                    self.state.bind_audio(locator, {"document_id": body["document_id"], "file_name": body["file_name"]})
                    return Response({})
                if handler == "update_play_config":
                    self.state.update_play_config(locator, body["play_config"])
                    return Response({})
            if path == f"/xblock/{VERTICAL}" and method == "PUT":
                self.state.reorder_children(VERTICAL, body["children"])
                return Response({})
            if path == f"/xblock/{GALLERY}" and method == "DELETE":
                self.state.delete_gallery(GALLERY)
                return Response({})
            return super().request(method, url, **kwargs)

    package = tmp_path / "package"
    package_at(package)
    state = ReplacementStudio()
    monkeypatch.setenv("FIRA_SAAS_OP_COOKIE", "csrftoken=test")
    monkeypatch.setattr(requests, "Session", lambda: ReplacementSession(state))
    monkeypatch.setitem(sys.modules, "oss2", types.SimpleNamespace(
        StsAuth=lambda *args: object(), Bucket=Bucket, determine_part_size=lambda *args, **kwargs: 1024 * 1024,
        SizedFileAdapter=lambda file, size: file, models=types.SimpleNamespace(PartInfo=lambda number, etag: (number, etag))))
    tenants = tmp_path / "tenants.json"
    write_json(tenants, {"tenants": {"test": {"studio_base": "https://studio.example.test", "courses_base": "https://courses.example.test"}}})
    result = run(package, RealStudio(TARGET, tenants))
    assert result["status"] == "success"
    assert state.deleted == [GALLERY] and state.published
    assert state.audio_bindings[NEW_GALLERY][0]["document_id"] == "12345678-1234-1234-1234-123456789abc.mp3"
