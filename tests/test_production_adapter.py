import copy
import json
import os
import sys
import threading
import time
import types
from urllib.parse import urlparse

import requests

from scripts.production import AgentBridge, Studio, synthesize as existing_synthesizer
from scripts.repair_one import repair_one
from test_repair_one import GALLERY, SEQUENTIAL, Studio as FakeStudio, package_at, synthesize, write_json


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, state):
        self.state = state
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()

    def get(self, url, **kwargs):
        path = urlparse(url).path
        if path.startswith("/xblock/"):
            return Response(self.state.get_block_details(path.removeprefix("/xblock/")))
        if kwargs.get("params") == {"format": "concise"}:
            return Response(self.state.get_course_structure("unused"))
        tree = self.state.get_course_structure("unused")
        section = tree["child_info"]["children"][0]["child_info"]["children"][0]
        section.update({"published": self.state.published, "has_changes": not self.state.published})
        return Response(tree)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        path = urlparse(url).path
        body = kwargs.get("json") or {}
        if path.endswith("/get_files"):
            return Response(self.state.get_files(GALLERY))
        if path.endswith("/get_play_config"):
            return Response(self.state.get_play_config(GALLERY))
        if path.endswith("/get_audios"):
            return Response(self.state.get_audios(GALLERY))
        if path.endswith("/get_upload_info"):
            return Response({"file_key": "audio/12345678-1234-1234-1234-123456789abc", "region": "oss-test", "bucket": "bucket", "Credentials": {"AccessKeyId": "key", "AccessKeySecret": "secret", "SecurityToken": "token"}})
        if path.endswith("/api/edxdal/v1/documents/"):
            return Response({})
        if path.endswith("/save_audio"):
            self.state.bind_audio(GALLERY, {"document_id": body["document_id"], "file_name": body["file_name"]})
            return Response({})
        if path.endswith("/update_play_config"):
            self.state.update_play_config(GALLERY, body["play_config"])
            return Response({})
        if path.endswith(SEQUENTIAL):
            self.state.publish_sequential(SEQUENTIAL)
            return Response({})
        raise AssertionError(f"unexpected network operation {method} {path}")


class Bucket:
    def __init__(self, *args):
        pass

    def init_multipart_upload(self, key):
        return types.SimpleNamespace(upload_id="upload")

    def upload_part(self, key, upload_id, number, data):
        return types.SimpleNamespace(etag="etag")

    def complete_multipart_upload(self, key, upload_id, parts):
        pass


def test_repair_uses_existing_studio_adapter_against_simulated_http_and_oss(tmp_path, monkeypatch):
    package = tmp_path / "package"
    package_at(package)
    state = FakeStudio()
    monkeypatch.setenv("FIRA_SAAS_OP_COOKIE", "csrftoken=test")
    monkeypatch.setattr(requests, "Session", lambda: FakeSession(state))
    monkeypatch.setitem(sys.modules, "oss2", types.SimpleNamespace(
        StsAuth=lambda *args: object(), Bucket=Bucket, determine_part_size=lambda *args, **kwargs: 1024 * 1024,
        SizedFileAdapter=lambda file, size: file, models=types.SimpleNamespace(PartInfo=lambda number, etag: (number, etag))))
    tenants = tmp_path / "tenants.json"
    write_json(tenants, {"tenants": {"test": {"studio_base": "https://studio.example.test", "courses_base": "https://courses.example.test"}}})
    target = "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113"
    studio = Studio(target, tenants)
    result = repair_one(package, target, "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=synthesize, studio=studio)
    assert result["status"] == "success"
    assert state.published and len(state.audios) == 1
    assert state.audios[0]["document_id"] == "12345678-1234-1234-1234-123456789abc.mp3"
    assert [row["subtitle"] for row in state.config["time_config"]] == ["# 测试", "第一段。", "第二段。"]


def test_real_adapter_detects_sibling_html_body_change_with_unchanged_concise_tree(tmp_path, monkeypatch):
    summary = "block-v1:FIRAx+TARGET+20261113+type@html+block@other_summary"
    sibling = "block-v1:FIRAx+TARGET+20261113+type@vertical+block@other_unit"

    class ContentStudio(FakeStudio):
        body = "<p>original sibling content</p>"

        def get_course_structure(self, course_id):
            tree = super().get_course_structure(course_id)
            section = tree["child_info"]["children"][0]["child_info"]["children"][0]
            section["child_info"]["children"].append({"category": "vertical", "id": sibling,
                "display_name": "在线训战", "child_info": {"children": [{"category": "html", "id": summary}]}})
            return tree

    class ContentSession(FakeSession):
        def get(self, url, **kwargs):
            path = urlparse(url).path
            if path.startswith("/xblock/"):
                locator = path.removeprefix("/xblock/")
                category = locator.split("+type@", 1)[1].split("+", 1)[0]
                return Response({"id": locator, "category": category,
                                 "data": self.state.body if locator == summary else "",
                                 "metadata": {}, "edited_on": "2026-11-13 10:00"})
            return super().get(url, **kwargs)

    package = tmp_path / "package"
    package_at(package)
    state = ContentStudio()
    monkeypatch.setenv("FIRA_SAAS_OP_COOKIE", "csrftoken=test")
    monkeypatch.setattr(requests, "Session", lambda: ContentSession(state))
    monkeypatch.setitem(sys.modules, "oss2", types.SimpleNamespace(
        StsAuth=lambda *args: object(), Bucket=Bucket, determine_part_size=lambda *args, **kwargs: 1024 * 1024,
        SizedFileAdapter=lambda file, size: file, models=types.SimpleNamespace(PartInfo=lambda number, etag: (number, etag))))
    tenants = tmp_path / "tenants.json"
    write_json(tenants, {"tenants": {"test": {"studio_base": "https://studio.example.test", "courses_base": "https://courses.example.test"}}})
    target = "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113"

    def concurrent_synthesis(manifest, voice, output):
        audio = synthesize(manifest, voice, output)
        state.body = "<p>concurrent sibling edit</p>"
        return audio

    result = repair_one(package, target, "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=concurrent_synthesis, studio=Studio(target, tenants))
    assert result["status"] != "success" and "changed" in result["reason"] and not state.published


def test_real_adapter_stops_for_component_with_unobservable_content_fields(tmp_path, monkeypatch):
    class VideoStudio(FakeStudio):
        def get_course_structure(self, course_id):
            tree = super().get_course_structure(course_id)
            unit = tree["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]
            unit["child_info"]["children"].append({"category": "video",
                "id": "block-v1:FIRAx+TARGET+20261113+type@video+block@video"})
            return tree

    package = tmp_path / "package"
    package_at(package)
    state = VideoStudio()
    monkeypatch.setenv("FIRA_SAAS_OP_COOKIE", "csrftoken=test")
    monkeypatch.setattr(requests, "Session", lambda: FakeSession(state))
    tenants = tmp_path / "tenants.json"
    write_json(tenants, {"tenants": {"test": {"studio_base": "https://studio.example.test", "courses_base": "https://courses.example.test"}}})
    target = "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113"
    result = repair_one(package, target, "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=synthesize, studio=Studio(target, tenants))
    assert result["status"] == "unconfirmed"
    assert not state.published and state.audios == [{"document_id": "old.mp3", "file_name": "old.mp3"}]


def test_real_adapter_stops_if_sibling_unit_children_are_not_readable(tmp_path, monkeypatch):
    class IncompleteStudio(FakeStudio):
        def get_course_structure(self, course_id):
            tree = super().get_course_structure(course_id)
            section = tree["child_info"]["children"][0]["child_info"]["children"][0]
            section["child_info"]["children"].append({"category": "vertical", "has_children": True,
                "id": "block-v1:FIRAx+TARGET+20261113+type@vertical+block@unreadable"})
            return tree

    package = tmp_path / "package"
    package_at(package)
    state = IncompleteStudio()
    monkeypatch.setenv("FIRA_SAAS_OP_COOKIE", "csrftoken=test")
    monkeypatch.setattr(requests, "Session", lambda: FakeSession(state))
    tenants = tmp_path / "tenants.json"
    write_json(tenants, {"tenants": {"test": {"studio_base": "https://studio.example.test", "courses_base": "https://courses.example.test"}}})
    target = "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113"
    result = repair_one(package, target, "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=synthesize, studio=Studio(target, tenants))
    assert result["status"] == "unconfirmed" and not state.published


def test_repair_uses_existing_tts_script_with_a_simulated_bl_boundary(tmp_path, monkeypatch):
    package = tmp_path / "package"
    package_at(package)
    manifest_path = package / "1.2 测试" / "imagesgallery" / "imagesgallery.json"
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    original["items"][1]["voice"] = "page-override"
    write_json(manifest_path, original)
    fake_bl = tmp_path / "fake_bl.py"
    fake_bl.write_text('import pathlib, subprocess, sys\nargs = sys.argv[1:]\nout = pathlib.Path(args[args.index("--out") + 1])\nout.with_suffix(".args.json").write_text(__import__("json").dumps(args), encoding="utf-8")\nsubprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "0.3", str(out)], check=True)\n', encoding="utf-8")
    if os.name == "nt":
        (tmp_path / "bl.cmd").write_text(f'@"{sys.executable}" "{fake_bl}" %*\n', encoding="utf-8")
    else:
        executable = tmp_path / "bl"
        executable.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake_bl}" "$@"\n', encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    studio = FakeStudio()
    target = "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113"
    result = repair_one(package, target, "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=existing_synthesizer, studio=studio)
    assert result["status"] == "success"
    audio = package / "repair" / "1.2" / "audio"
    calls = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((audio / "segments").glob("*.args.json"))]
    assert len(calls) == 3
    assert [call[call.index("--voice") + 1] for call in calls] == ["voice-old", "page-override", "voice-old"]
    assert all(call[call.index("--model") + 1] == "model-old" and call[call.index("--rate") + 1] == "1.1" for call in calls)
    assert [(audio / "texts" / f"page-{n:03d}.txt").read_text(encoding="utf-8") for n in range(1, 4)] == ["测试", "第一段。", "第二段。"]


def test_repair_exchanges_separate_section_context_with_split_and_review_agents(tmp_path):
    package = tmp_path / "package"
    package_at(package)
    bridge = AgentBridge(package / "repair" / ".agent-bridge" / "1.2", timeout_seconds=5)
    received = []
    stop = threading.Event()

    def agent():
        while not stop.is_set():
            for request in bridge.path.glob("*.request.json"):
                response = request.with_name(request.name.replace(".request.json", ".response.json"))
                if response.exists():
                    continue
                data = json.loads(request.read_text(encoding="utf-8"))
                received.append((request.name.split("-")[0], data))
                result = {"pages": ["# 测试", "第一段。", "第二段。"]} if request.name.startswith("split-") else {"approved": True}
                temp = response.with_suffix(".tmp")
                temp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                temp.replace(response)
            time.sleep(0.02)

    thread = threading.Thread(target=agent)
    thread.start()
    try:
        target = "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113"
        result = repair_one(package, target, "1.2", package / "repair", split=bridge.split, review=bridge.review, synthesize=synthesize, studio=FakeStudio())
    finally:
        stop.set()
        thread.join()
    assert result["status"] == "success"
    assert [kind for kind, _ in received] == ["split", "review"]
    assert all("错误正文" not in context["source"] and len(context["images"]) == 3 for _, context in received)
    assert "pages" not in received[0][1] and received[1][1]["pages"] == ["# 测试", "第一段。", "第二段。"]
