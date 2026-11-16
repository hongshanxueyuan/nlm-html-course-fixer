import copy
import json
import os
from pathlib import Path
import sys
import threading
import time
import types
from urllib.parse import urlparse

import pytest
import requests

from scripts import production, run_repair
from scripts.production import AgentBridge, Studio, synthesize as existing_synthesizer
from scripts.repair_one import _target, repair_one
from test_repair_one import GALLERY, SEQUENTIAL, VERTICAL, Studio as FakeStudio, fixture, package_at, synthesize, write_json


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
        section.update({"published": True, "has_changes": not self.state.published})
        for vertical in section["child_info"]["children"]:
            if vertical.get("id") == VERTICAL:
                vertical.update({"published": self.state.published, "has_changes": not self.state.published})
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
        if path.endswith(VERTICAL):
            self.state.publish_vertical(VERTICAL)
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


def test_cli_reuses_tenant_config_in_existing_repair_directory(tmp_path, monkeypatch):
    package = tmp_path / "package"
    repair = package / "nlm-html-course-fixer"
    tenants = repair / "tenant-config.json"
    tenants.parent.mkdir(parents=True)
    write_json(tenants, {"tenants": {"uat": {"studio_base": "https://studio.uat.firacademy.com"}}})
    target = "https://studio.uat.firacademy.com/course/course-v1:FIRAx+TARGET+20261113"
    seen = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_repair", "--package", str(package), "--studio-url", target, "--first-chapter"])
    monkeypatch.setattr(run_repair, "Studio", lambda url, config: seen.update(url=url, config=config))
    monkeypatch.setattr(run_repair, "run_batch", lambda *args, **kwargs: {"status": "success"})

    assert run_repair.main() == 0
    assert Path(seen["config"]).resolve() == tenants.resolve()


def test_cli_has_known_uat_profile_without_a_user_config(tmp_path, monkeypatch):
    package = tmp_path / "package"
    package.mkdir()
    target = "https://studio.uat.firacademy.com/course/course-v1:FIRAx+TARGET+20261113"
    seen = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_repair", "--package", str(package), "--studio-url", target, "--first-chapter"])
    monkeypatch.setattr(run_repair, "Studio", lambda url, config: seen.update(url=url, config=config))
    monkeypatch.setattr(run_repair, "run_batch", lambda *args, **kwargs: {"status": "success"})

    assert run_repair.main() == 0
    _, _, studio, courses = production._auditor_inputs().resolve_audit_input(
        tenant=None, course_id=None, studio_url=target, tenant_config_path=Path(seen["config"]))
    assert (studio, courses) == ("https://studio.uat.firacademy.com", "https://courses.uat.firacademy.com")


def test_tenant_discovery_does_not_fall_back_for_an_explicit_missing_file(tmp_path):
    target = "https://studio.firacademy.com/course/course-v1:FIRAx+TARGET+20261113"
    with pytest.raises(FileNotFoundError):
        production.find_tenant_config(target, tmp_path, tmp_path / "repair", tmp_path / "missing.json")


def test_unknown_target_host_requires_a_matching_tenant_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = "https://studio.unknown.example/course/course-v1:FIRAx+TARGET+20261113"
    with pytest.raises(ValueError, match="tenant profile"):
        production.find_tenant_config(target, tmp_path, tmp_path / "repair")


def test_studio_reuses_persistent_windows_credentials_when_process_has_none(tmp_path, monkeypatch):
    for name in production.STUDIO_AUTH_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(production, "_persistent_auth_environments", lambda: [{"FIRA_SAAS_OP_COOKIE": "csrftoken=test"}])
    monkeypatch.setattr(requests, "Session", lambda: FakeSession(FakeStudio()))
    tenants = tmp_path / "tenants.json"
    write_json(tenants, {"tenants": {"test": {"studio_base": "https://studio.example.test"}}})

    studio = Studio("https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113", tenants)

    assert studio.client.csrf_token == "test"


def test_process_auth_is_not_mixed_with_persistent_auth(monkeypatch):
    for name in production.STUDIO_AUTH_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FIRA_SAAS_OP_ACCOUNT", "process-account")
    monkeypatch.setattr(production, "_persistent_auth_environments", lambda: [{"FIRA_SAAS_OP_ACCOUNT": "other-account", "FIRA_SAAS_OP_PASSWORD": "other-password"}])

    env = production._studio_auth_environment()
    assert env["FIRA_SAAS_OP_ACCOUNT"] == "process-account"
    assert "FIRA_SAAS_OP_PASSWORD" not in env


@pytest.mark.skipif(os.name != "nt", reason="Windows persistent environment")
def test_windows_persistent_environment_reads_user_and_machine_scopes(monkeypatch):
    import winreg

    class Key:
        def __init__(self, root): self.root = root
        def __enter__(self): return self
        def __exit__(self, *args): return False

    values = {winreg.HKEY_CURRENT_USER: {"FIRA_SAAS_OP_ACCOUNT": "account", "FIRA_SAAS_OP_PASSWORD": "password"},
              winreg.HKEY_LOCAL_MACHINE: {"FIRA_SAAS_OP_COOKIE": "csrftoken=machine"}}
    monkeypatch.setattr(winreg, "OpenKey", lambda root, path: Key(root))
    def query(handle, name):
        if name not in values[handle.root]:
            raise FileNotFoundError(name)
        return values[handle.root][name], winreg.REG_SZ
    monkeypatch.setattr(winreg, "QueryValueEx", query)

    assert production._persistent_auth_environments() == [values[winreg.HKEY_CURRENT_USER], values[winreg.HKEY_LOCAL_MACHINE]]


def test_agent_bridge_keeps_review_feedback(tmp_path, monkeypatch):
    bridge = AgentBridge(tmp_path)
    monkeypatch.setattr(bridge, "_call", lambda kind, request: {"approved": False, "reason": "wrong boundary"})
    assert bridge.review({}) == {"approved": False, "reason": "wrong boundary"}


def test_tts_subprocess_inherits_bl_config_with_script_attribution(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"audio": {"full_audio": str(tmp_path / "audio.mp3")}})
    seen = {}
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("BAILIAN_SKILL_NAME", "unrelated")
    monkeypatch.setattr(production.subprocess, "run", lambda args, **kwargs: seen.update(kwargs))

    result = existing_synthesizer(manifest, {"voice": "voice", "model": "model", "rate": 1.1, "gap_seconds_between_segments": 1}, tmp_path)

    assert result == tmp_path / "audio.mp3"
    assert seen["env"]["DASHSCOPE_API_KEY"] == "test-key"
    assert seen["env"]["BAILIAN_CALLER_TYPE"] == "script"
    assert seen["env"]["BAILIAN_CALLER_NAME"] == "codex"
    assert "BAILIAN_SKILL_NAME" not in seen["env"]


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


def test_real_adapter_ignores_sibling_html_body_change(tmp_path, monkeypatch):
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
    assert result["status"] == "success" and state.published


@pytest.mark.parametrize(("category", "locator", "expected"), [
    ("video", "block-v1:FIRAx+TARGET+20261113+type@video+block@video", "type@video+block@video"),
    ("video?token=secret", "block-v1:FIRAx+TARGET+20261113+type@video+block@video?token=secret", "unknown"),
])
def test_real_adapter_stops_for_component_with_unobservable_content_fields(tmp_path, monkeypatch, category, locator, expected):
    class VideoStudio(FakeStudio):
        def get_course_structure(self, course_id):
            tree = super().get_course_structure(course_id)
            unit = tree["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]
            unit["child_info"]["children"].append({"category": category, "id": locator})
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
    assert expected in result["reason"] and "secret" not in result["reason"]
    assert not state.published and state.audios == [{"document_id": "old.mp3", "file_name": "old.mp3"}]


def test_real_adapter_ignores_unreadable_sibling_unit_children(tmp_path, monkeypatch):
    class IncompleteStudio(FakeStudio):
        def get_course_structure(self, course_id):
            tree = super().get_course_structure(course_id)
            section = tree["child_info"]["children"][0]["child_info"]["children"][0]
            section["child_info"]["children"].append(fixture("studio_empty_vertical.json"))
            return tree

    package = tmp_path / "package"
    package_at(package)
    state = IncompleteStudio()
    monkeypatch.setenv("FIRA_SAAS_OP_COOKIE", "csrftoken=test")
    monkeypatch.setattr(requests, "Session", lambda: FakeSession(state))
    tenants = tmp_path / "tenants.json"
    write_json(tenants, {"tenants": {"test": {"studio_base": "https://studio.example.test", "courses_base": "https://courses.example.test"}}})
    target = "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113"
    result = _target(Studio(target, tenants), "course-v1:FIRAx+TARGET+20261113", "第一章", "1.2 测试")
    assert result[:3] == (SEQUENTIAL, VERTICAL, GALLERY) and not state.published


@pytest.mark.parametrize("shape", [
    {"has_children": False},
    {"has_children": False, "child_info": {"children": []}},
    {"has_children": True, "child_info": {"children": []}},
])
def test_real_adapter_accepts_explicitly_empty_sibling_unit(tmp_path, monkeypatch, shape):
    class EmptyStudio(FakeStudio):
        def get_course_structure(self, course_id):
            tree = super().get_course_structure(course_id)
            section = tree["child_info"]["children"][0]["child_info"]["children"][0]
            section["child_info"]["children"].append({"category": "vertical", **shape,
                "id": "block-v1:FIRAx+TARGET+20261113+type@vertical+block@empty"})
            return tree

    package = tmp_path / "package"
    package_at(package)
    state = EmptyStudio()
    monkeypatch.setenv("FIRA_SAAS_OP_COOKIE", "csrftoken=test")
    monkeypatch.setattr(requests, "Session", lambda: FakeSession(state))
    monkeypatch.setitem(sys.modules, "oss2", types.SimpleNamespace(
        StsAuth=lambda *args: object(), Bucket=Bucket, determine_part_size=lambda *args, **kwargs: 1024 * 1024,
        SizedFileAdapter=lambda file, size: file, models=types.SimpleNamespace(PartInfo=lambda number, etag: (number, etag))))
    tenants = tmp_path / "tenants.json"
    write_json(tenants, {"tenants": {"test": {"studio_base": "https://studio.example.test", "courses_base": "https://courses.example.test"}}})
    target = "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113"
    result = repair_one(package, target, "1.2", package / "repair", split=lambda request: ["# 测试", "第一段。", "第二段。"], review=lambda request: True, synthesize=synthesize, studio=Studio(target, tenants))
    assert result["status"] == "success" and state.published


@pytest.mark.parametrize("shape", [
    {"has_children": False, "child_info": {"children": [
        {"category": "html", "id": "block-v1:FIRAx+TARGET+20261113+type@html+block@extra"}]}},
    {"has_children": "false"},
    {},
])
def test_real_adapter_ignores_ambiguous_sibling_unit(tmp_path, monkeypatch, shape):
    class AmbiguousStudio(FakeStudio):
        def get_course_structure(self, course_id):
            tree = super().get_course_structure(course_id)
            section = tree["child_info"]["children"][0]["child_info"]["children"][0]
            section["child_info"]["children"].append({"category": "vertical", **shape,
                "id": "block-v1:FIRAx+TARGET+20261113+type@vertical+block@ambiguous"})
            return tree

    package = tmp_path / "package"
    package_at(package)
    state = AmbiguousStudio()
    monkeypatch.setenv("FIRA_SAAS_OP_COOKIE", "csrftoken=test")
    monkeypatch.setattr(requests, "Session", lambda: FakeSession(state))
    tenants = tmp_path / "tenants.json"
    write_json(tenants, {"tenants": {"test": {"studio_base": "https://studio.example.test", "courses_base": "https://courses.example.test"}}})
    target = "https://studio.example.test/course/course-v1:FIRAx+TARGET+20261113"
    result = _target(Studio(target, tenants), "course-v1:FIRAx+TARGET+20261113", "第一章", "1.2 测试")
    assert result[:3] == (SEQUENTIAL, VERTICAL, GALLERY) and not state.published


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
