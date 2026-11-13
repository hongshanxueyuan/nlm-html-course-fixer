"""Adapters around existing NLM, Gallery-audio, and Studio tools."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
from pathlib import Path
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


class Studio:
    def __init__(self, target_url, tenant_config):
        auditor = _root("NLM_COURSE_AUDITOR_ROOT", "nlm-course-auditor")
        _module("fixer_auditor", auditor / "scripts" / "__init__.py", package=True)
        inputs = importlib.import_module("fixer_auditor.inputs")
        studio = importlib.import_module("fixer_auditor.studio")
        self.audit = importlib.import_module("fixer_auditor.audit")
        _, self.course_id, self.base, courses = inputs.resolve_audit_input(tenant=None, course_id=None, studio_url=target_url, tenant_config_path=Path(tenant_config).resolve())
        self.client = studio.StudioApiClient(studio_base=self.base, courses_base=courses, course_id=self.course_id)
        publisher_root = _root("STUDIO_IMAGEGALLERY_PUBLISH_ROOT", "studio-imagegallery-publish")
        self.publisher = _module("fixer_publisher", publisher_root / "scripts" / "publish_imagegallery_block.py")
        self.adapter = self.publisher.load_adapter(str(publisher_root / "references" / "adapter.template.json"))

    def __getattr__(self, name):
        return getattr(self.client, name)

    def get_course_publication_structure(self, course_id):
        return self.audit.sequential_publication_sections(self.client.get_course_publication_structure(course_id))

    def _context(self, gallery_id):
        return {"studio_base": self.base, "block_locator": gallery_id,
                "block_url": f"{self.base}/container/{gallery_id}", "csrftoken": self.client._csrf()}

    def _call(self, name, context):
        return self.publisher.call_template(self.client.session, name, self.adapter[name], context, True, 60)

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
    subprocess.run([sys.executable, str(script), "--manifest", str(manifest), "--out-dir", str(output),
                    "--voice", voice["voice"], "--model", voice["model"], "--rate", str(voice["rate"]),
                    "--gap-seconds", str(voice["gap_seconds_between_segments"])], check=True, capture_output=True)
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
        return self._call("review", request).get("approved") is True
