"""Adapters around existing NLM, Gallery-audio, and Studio tools."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


def _root(variable, sibling):
    return Path(os.environ.get(variable, Path(__file__).resolve().parents[2] / sibling)).resolve()


def _module(name, path, *, package=False):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(path.parent)] if package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


STUDIO_AUTH_VARS = ("FIRA_SAAS_OP_ACCOUNT", "FIRA_SAAS_OP_PASSWORD", "FIRA_SAAS_OP_COOKIE", "FIRA_SAAS_OP_CSRF_TOKEN")


def _auditor_inputs():
    auditor = _root("NLM_COURSE_AUDITOR_ROOT", "nlm-course-auditor")
    _module("fixer_auditor", auditor / "scripts" / "__init__.py", package=True)
    return importlib.import_module("fixer_auditor.inputs")


def find_tenant_config(target_url, package, repair_dir, explicit=None):
    """Reuse a matching auditor config, falling back to known FIRA hosts."""
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if not candidate.is_file():
            raise FileNotFoundError("Explicit tenant configuration does not exist")
        return candidate
    inputs = _auditor_inputs()
    candidates = [Path.cwd() / "tenant-config.json", Path(repair_dir) / "tenant-config.json",
                  Path(package) / "tenant-config.json", Path(__file__).resolve().parents[1] / "references" / "tenant-config.json"]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            inputs.resolve_audit_input(tenant=None, course_id=None, studio_url=target_url,
                                       tenant_config_path=candidate.resolve())
        except inputs.AuditError as exc:
            if str(exc) == "Studio URL host does not match a configured tenant profile.":
                continue
            raise
        return candidate.resolve()
    raise ValueError("Studio URL host does not match an available tenant profile; supply --tenant-config")


def _persistent_auth_environments():
    """Read the existing Windows user/machine environment without exposing values."""
    if os.name != "nt":
        return []
    import winreg

    environments = []
    for root, key in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        values = {}
        try:
            with winreg.OpenKey(root, key) as handle:
                for name in STUDIO_AUTH_VARS:
                    try:
                        value, _ = winreg.QueryValueEx(handle, name)
                    except FileNotFoundError:
                        continue
                    if isinstance(value, str) and value.strip():
                        values[name] = value
        except OSError:
            pass
        environments.append(values)
    return environments


def _studio_auth_environment():
    env = os.environ.copy()
    if any(env.get(name, "").strip() for name in STUDIO_AUTH_VARS):
        return env
    for values in _persistent_auth_environments():
        if any(values.get(name, "").strip() for name in STUDIO_AUTH_VARS):
            env.update({name: values[name] for name in STUDIO_AUTH_VARS if values.get(name, "").strip()})
            break
    return env


class Studio:
    def __init__(self, target_url, tenant_config):
        inputs = _auditor_inputs()
        studio = importlib.import_module("fixer_auditor.studio")
        self.audit = importlib.import_module("fixer_auditor.audit")
        _, self.course_id, self.base, courses = inputs.resolve_audit_input(tenant=None, course_id=None, studio_url=target_url, tenant_config_path=Path(tenant_config).resolve())
        self.client = studio.StudioApiClient(studio_base=self.base, courses_base=courses, course_id=self.course_id,
                                             environ=_studio_auth_environment())
        publisher_root = _root("STUDIO_IMAGEGALLERY_PUBLISH_ROOT", "studio-imagegallery-publish")
        self.publisher = _module("fixer_publisher", publisher_root / "scripts" / "publish_imagegallery_block.py")
        self.adapter = self.publisher.load_adapter(str(publisher_root / "references" / "adapter.template.json"))

    def __getattr__(self, name):
        return getattr(self.client, name)

    def get_course_publication_structure(self, course_id):
        return self.audit.sequential_publication_sections(self.client.get_course_publication_structure(course_id))

    def get_block_details(self, locator):
        prefix = "block-v1:" + self.course_id.removeprefix("course-v1:") + "+type@"
        if not isinstance(locator, str) or not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]+\+block@[A-Za-z0-9_.~-]+", locator):
            raise ValueError("block locator does not belong to target course")
        response = self.client.session.get(
            f"{self.base}/xblock/{locator}", headers={"Accept": "application/json"},
            timeout=60, allow_redirects=False,
        )
        return self.client._json_response(response, "xblock_content")

    def _context(self, gallery_id):
        return {"studio_base": self.base, "block_locator": gallery_id,
                "block_url": f"{self.base}/container/{gallery_id}", "csrftoken": self.client._csrf()}

    def _call(self, name, context):
        return self.publisher.call_template(self.client.session, name, self.adapter[name], context, True, 60)

    def create_gallery(self, vertical_id):
        context = self._context(vertical_id)
        context.update({"vertical_block_id": vertical_id, "imagegallery_category": "imagesgallery"})
        self._call("create_block", context)
        return context.get("block_locator")

    def upload_image(self, gallery_id, path):
        context = self._context(gallery_id)
        context["image_file"] = str(Path(path).resolve())
        return self._call("upload_image", context).get("asset_id")

    def order_images(self, gallery_id, ids):
        context = self._context(gallery_id)
        context["uploaded_asset_ids"] = ids
        self._call("change_files_order", context)

    def reorder_children(self, vertical_id, ids):
        context = self._context(vertical_id)
        context.update({"vertical_block_id": vertical_id, "vertical_children_obj": ids})
        self._call("reorder_children", context)

    def delete_gallery(self, gallery_id):
        context = self._context(gallery_id)
        context["delete_block_locator"] = gallery_id
        self._call("delete_block", context)

    def upload_audio(self, gallery_id, audio_path):
        import oss2  # noqa: F401; require the SDK before the publisher can install anything

        audio_path = Path(audio_path).resolve()
        if audio_path.suffix.lower() != ".mp3":
            raise ValueError("only MP3 can be uploaded")
        context = self._context(gallery_id)
        context.update({"audio_file": str(audio_path), "audio_file_name": f"repair-{uuid.uuid4().hex}.mp3",
                        "audio_post_fix": "mp3", "course_key": self.course_id,
                        "courses_json": json.dumps({self.course_id: False})})
        self._call("upload_audio_prepare", context)
        context["edx_document_id"] = self.publisher.maybe_extract_uuid(str(context.get("file_key", "")))
        if not context["edx_document_id"]:
            raise ValueError("audio upload returned no document identity")
        context["audio_document_id"] = context["edx_document_id"] + ".mp3"
        self.publisher.sts_multipart_upload_audio(context)
        self._call("upload_audio_register", context)
        return {"document_id": context["audio_document_id"], "file_name": context["audio_file_name"]}

    def bind_audio(self, gallery_id, binding):
        context = self._context(gallery_id)
        context.update({"audio_document_id": binding["document_id"], "audio_file_name": binding["file_name"]})
        self._call("save_audio_binding", context)


def synthesize(manifest, voice, output):
    root = _root("PPT_TO_IMAGESGALLERY_ROOT", "ppt-to-imagesgallery")
    script = root / "scripts" / "synthesize_imagesgallery_audio.py"
    env = os.environ.copy()
    env.update({"BAILIAN_CALLER_TYPE": "script", "BAILIAN_CALLER_NAME": "codex"})
    env.pop("BAILIAN_SKILL_NAME", None)
    env.pop("BAILIAN_SKILL_VERSION", None)
    subprocess.run([sys.executable, str(script), "--manifest", str(manifest), "--out-dir", str(output),
                    "--voice", voice["voice"], "--model", voice["model"], "--rate", str(voice["rate"]),
                    "--gap-seconds", str(voice["gap_seconds_between_segments"])], check=True, capture_output=True, env=env)
    data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    return Path(data["audio"]["full_audio"])


class AgentBridge:
    """Exchange one section's request with the host's isolated subagents."""
    def __init__(self, path, timeout_seconds=900):
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds

    def _call(self, kind, request):
        self.path.mkdir(parents=True, exist_ok=True)
        identity = f"{kind}-{uuid.uuid4().hex}"
        request_path = self.path / f"{identity}.request.json"
        response_path = self.path / f"{identity}.response.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"agent_request": kind, "request": str(request_path), "response": str(response_path)}), flush=True)
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if response_path.is_file():
                return json.loads(response_path.read_text(encoding="utf-8"))
            time.sleep(0.5)
        raise TimeoutError("isolated Agent response not received")

    def split(self, request):
        return self._call("split", request).get("pages")

    def review(self, request):
        return self._call("review", request)
