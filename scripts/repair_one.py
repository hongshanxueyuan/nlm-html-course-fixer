"""Single-section repair. Model, TTS and Studio are external boundaries."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlparse


class RepairError(Exception):
    pass


def _nlm():
    root = Path(os.environ.get("NLM_COURSE_SLIDES_ROOT", Path(__file__).resolve().parents[2] / "nlm-course-slides"))
    path = root / "scripts" / "common.py"
    if not path.is_file():
        raise RepairError("Set NLM_COURSE_SLIDES_ROOT to the existing nlm-course-slides checkout")
    name = "nlm_course_slides_common"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _children(node):
    info = node.get("child_info", {}) if isinstance(node, dict) else {}
    return info.get("children", []) if isinstance(info, dict) else []


def _one(items, label):
    if len(items) != 1:
        raise RepairError(f"{label} is missing or ambiguous")
    return items[0]


def _load_inputs(package, section_id, repair_dir):
    lists = []
    for path in package.rglob("section-list.json"):
        if path.resolve().is_relative_to(repair_dir):
            continue
        data = _json(path)
        lists.extend((path, item) for item in data.get("sections", []) if item.get("section_id") == section_id and item.get("block_type") == "html")
    _, section = _one(lists, "HTML section-list entry")
    title = section.get("section_title")
    sources = {}
    source_paths = [*package.rglob("course-content.json"), *package.rglob("fira_course-*.json")]
    for path in source_paths:
        if path.resolve().is_relative_to(repair_dir):
            continue
        data = _json(path)
        for chapter in data.get("chapters", []):
            for candidate in chapter.get("sections", []):
                if candidate.get("name") == title:
                    entry = (data.get("course_id"), chapter.get("name"), candidate)
                    sources[json.dumps(entry, ensure_ascii=False, sort_keys=True)] = entry
    source_id, chapter_name, source = _one(list(sources.values()), "source course section")
    if not source_id or not chapter_name:
        raise RepairError("source identity is incomplete")
    vertical = _one([v for v in source.get("verticals", []) if v.get("name") == "赋能内容"], "source 赋能内容")
    block = _one([b for b in vertical.get("blocks", []) if b.get("category") == "html" and b.get("name") == "文字讲解" and b.get("text")], "source 文字讲解")
    galleries = []
    for path in package.rglob("imagesgallery.json"):
        if path.resolve().is_relative_to(repair_dir):
            continue
        data = _json(path)
        if data.get("section_id") == section_id and data.get("section_title") == title:
            galleries.append((path, data))
    gallery_path, gallery = _one(galleries, "local Gallery")
    items = gallery.get("items")
    if not isinstance(items, list) or len(items) < 2 or [x.get("page") for x in items] != list(range(1, len(items) + 1)):
        raise RepairError("Gallery page order is invalid")
    images = [(gallery_path.parent / x.get("image", "")).resolve() for x in items]
    if any(not path.is_file() or not path.is_relative_to(package) for path in images) or len({p.name for p in images}) != len(images):
        raise RepairError("Gallery image missing")
    audio = gallery.get("audio", {})
    keys = ("voice", "model", "rate", "gap_seconds_between_segments")
    if any(k not in audio or audio[k] is None or audio[k] == "" for k in keys):
        raise RepairError("original audio configuration is incomplete")
    if not isinstance(audio["voice"], str) or not isinstance(audio["model"], str) or any(isinstance(audio[k], bool) or not isinstance(audio[k], (int, float)) for k in ("rate", "gap_seconds_between_segments")) or audio["rate"] <= 0 or audio["gap_seconds_between_segments"] < 0:
        raise RepairError("original audio configuration is invalid")
    return section, source_id, chapter_name, block, gallery, images, {key: audio[key] for key in keys}


def _target(studio, course_id, chapter_name, section_title):
    root = studio.get_course_structure(course_id)
    chapter = _one([x for x in _children(root) if x.get("category") == "chapter" and x.get("display_name", "").strip() == chapter_name], "target chapter")
    sequential = _one([x for x in _children(chapter) if x.get("category") == "sequential" and x.get("display_name", "").strip() == section_title], "target sequential")
    vertical = _one([x for x in _children(sequential) if x.get("category") == "vertical" and x.get("display_name", "").strip() == "赋能内容"], "target 赋能内容")
    gallery = _one([x for x in _children(vertical) if x.get("category") == "imagesgallery"], "target Gallery")
    locator = lambda node: node.get("id") or node.get("block_location") or node.get("locator")
    ids = [locator(sequential), locator(vertical), locator(gallery)]
    prefix = "block-v1:" + course_id.removeprefix("course-v1:") + "+type@"
    if any(not isinstance(locator, str) or not locator.startswith(prefix + kind + "+block@") or not locator.removeprefix(prefix + kind + "+block@") for locator, kind in zip(ids, ("sequential", "vertical", "imagesgallery"))):
        raise RepairError("target locators do not belong to the requested course")
    children = tuple((x.get("category"), locator(x)) for x in _children(vertical))
    if any(not category or not child_id for category, child_id in children):
        raise RepairError("target unit structure is incomplete")
    return *ids, children


def _files(studio, gallery_id):
    payload = studio.get_files(gallery_id)
    files = payload.get("files")
    if not isinstance(files, list) or isinstance(payload.get("total_count"), bool) or payload.get("total_count") != len(files):
        raise RepairError("Gallery file listing is incomplete")
    normalized = []
    for item in files:
        if not isinstance(item, dict):
            raise RepairError("Gallery file listing is incomplete")
        identity = str(item.get("id") or item.get("asset_key") or "").strip()
        name = str(item.get("display_name") or item.get("name") or item.get("filename") or item.get("file_name") or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not identity or not name:
            raise RepairError("Gallery file listing is incomplete")
        normalized.append({"id": identity, "filename": name})
    if len({item["id"] for item in normalized}) != len(normalized):
        raise RepairError("Gallery asset identities are ambiguous")
    return normalized


def _audio(studio, gallery_id):
    value = studio.get_audios(gallery_id).get("audios")
    if not isinstance(value, list) or len(value) != 1 or not value[0].get("document_id") or not value[0].get("file_name"):
        raise RepairError("Gallery audio identity is not recoverable")
    return {key: value[0][key] for key in ("document_id", "file_name")}


def _config(studio, gallery_id):
    value = studio.get_play_config(gallery_id).get("play_config")
    if not isinstance(value, dict) or not isinstance(value.get("time_config"), list):
        raise RepairError("Gallery play config is unreadable")
    return value


def _replace(read, write, original, desired):
    for _ in range(3):
        current = read()
        if current == desired:
            return
        if current != original:
            raise RepairError("target changed during write")
        try:
            write(copy.deepcopy(desired))
        except Exception:
            pass
        current = read()
        if current == desired:
            return
        if current != original:
            raise RepairError("write outcome is uncertain")
    raise RepairError("write retries exhausted")


def _validate_audio(manifest_path, pages, audio_path):
    data = _json(manifest_path)
    rows = data.get("items", [])
    if [x.get("page") for x in rows] != list(range(1, len(pages) + 1)) or [x.get("subtitle") for x in rows] != pages:
        raise RepairError("generated subtitles differ from approved pages")
    cursor = 0
    for row in rows:
        start, end = row.get("start"), row.get("end")
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int) or start != cursor or end <= start:
            raise RepairError("invalid generated audio timeline")
        cursor = end
    audio_path = Path(audio_path).resolve()
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)], capture_output=True, text=True, check=True)
    duration = float(probe.stdout.strip())
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(audio_path), "-f", "null", "-"], capture_output=True, check=True)
    if duration <= 0 or cursor > (duration + 0.25) * 1000:
        raise RepairError("timeline exceeds decodable audio")
    return [{key: item[key] for key in ("page", "start", "end", "subtitle")} for item in rows]


def repair_one(package, target_url, section_id, repair_dir, *, split, review, synthesize, studio):
    """Run one repair. Callbacks are isolated Agent, TTS and Studio boundaries."""
    package, repair_dir = Path(package).resolve(), Path(repair_dir).resolve()
    if not repair_dir.is_relative_to(package) or repair_dir == package:
        raise RepairError("repair directory must be a dedicated child of the course package")
    if not isinstance(section_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", section_id) or ".." in section_id:
        raise RepairError("invalid section identifier")
    parsed = urlparse(target_url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or not re.fullmatch(r"/course/course-v1:[^/\s+]+\+[^/\s+]+\+[^/\s+]+/?", parsed.path) or parsed.query or parsed.fragment:
        raise RepairError("target must be an explicit Studio course URL without credentials")
    course_id = parsed.path.removeprefix("/course/").rstrip("/")
    marker = repair_dir / ".nlm-html-course-fixer"
    if repair_dir.exists() and not marker.is_file() and any(repair_dir.iterdir()):
        raise RepairError("repair directory is not dedicated to this skill")
    repair_dir.mkdir(parents=True, exist_ok=True)
    marker.touch(exist_ok=True)
    report = {"section_id": section_id, "target_url": target_url, "status": "failed", "phase": "inputs", "recovery": "not needed"}
    output = repair_dir / section_id
    old_audio = new_audio = old_config = new_config = None
    gallery_id = None
    try:
        section, source_id, chapter_name, block, gallery, images, voice = _load_inputs(package, section_id, repair_dir)
        report["source_course_id"] = source_id
        common = _nlm()
        source = common._clean_page_body(common._render_html(block["text"]))
        cover_title = common.strip_section_prefix(section["section_title"])
        body_parts = common._remove_duplicate_cover_heading([source], title=cover_title)
        body = body_parts[0] if body_parts else ""
        request = {"source": source, "body": body, "cover_title": cover_title, "old_pages": section.get("page_content", []), "images": [str(p) for p in images], "count": len(images)}
        report["phase"] = "pagination"
        pages = None
        for _ in range(3):
            try:
                proposal = split(copy.deepcopy(request))
                if not isinstance(proposal, list) or len(proposal) != len(images) or proposal[0] != f"# {cover_title}":
                    raise RepairError("incorrect page count or cover")
                proposal = common._normalize_explicit_page_content(proposal)
                common._validate_html_pagination_source_coverage(body, proposal[1:])
                if review({**copy.deepcopy(request), "pages": copy.deepcopy(proposal)}) is not True:
                    raise RepairError("independent review rejected pages")
                pages = proposal
                break
            except Exception:
                continue
        if pages is None:
            raise RepairError("pagination or review did not pass")
        report["phase"] = "target"
        target = _target(studio, course_id, chapter_name, section["section_title"])
        sequential_id, vertical_id, gallery_id, children = target
        report.update({"sequential": sequential_id, "vertical": vertical_id, "gallery": gallery_id})
        files = _files(studio, gallery_id)
        if [x["filename"] for x in files] != [p.name for p in images]:
            raise RepairError("Gallery images differ; replacement path required")
        old_audio, old_config = _audio(studio, gallery_id), _config(studio, gallery_id)
        _write(output / "old-state.json", {"audio": old_audio, "play_config": old_config, "files": files, "children": children})
        items = []
        for index, (old, image, text) in enumerate(zip(gallery["items"], images, pages), start=1):
            item = {"page": index, "image": os.path.relpath(image, output).replace("\\", "/"), "speech": text}
            item.update({key: old[key] for key in ("voice", "voice_preset") if key in old})
            items.append(item)
        manifest = {key: value for key, value in gallery.items() if key not in {"audio", "items", "source_speech", "voice", "voice_preset"}}
        manifest["items"] = items
        manifest_path = output / "imagesgallery.json"
        _write(manifest_path, manifest)
        report["phase"] = "audio"
        audio_path = synthesize(manifest_path, copy.deepcopy(voice), output / "audio")
        if not Path(audio_path).resolve().is_relative_to((output / "audio").resolve()):
            raise RepairError("generated audio is outside the repair directory")
        time_config = _validate_audio(manifest_path, pages, audio_path)
        new_config = copy.deepcopy(old_config)
        new_config["time_config"] = time_config
        if _target(studio, course_id, chapter_name, section["section_title"]) != target or _files(studio, gallery_id) != files or _audio(studio, gallery_id) != old_audio or _config(studio, gallery_id) != old_config:
            raise RepairError("target changed before update")
        report["phase"] = "update"
        new_audio = studio.upload_audio(gallery_id, audio_path)
        if not isinstance(new_audio, dict) or not new_audio.get("document_id") or not new_audio.get("file_name") or new_audio == old_audio:
            raise RepairError("new audio identity is missing")
        if _target(studio, course_id, chapter_name, section["section_title"]) != target or _files(studio, gallery_id) != files:
            report["status"] = "unconfirmed"
            raise RepairError("target changed during audio upload")
        _replace(lambda: _audio(studio, gallery_id), lambda value: studio.bind_audio(gallery_id, value), old_audio, new_audio)
        if _target(studio, course_id, chapter_name, section["section_title"]) != target or _files(studio, gallery_id) != files:
            report["status"] = "unconfirmed"
            raise RepairError("target changed during audio binding")
        _replace(lambda: _config(studio, gallery_id), lambda value: studio.update_play_config(gallery_id, value), old_config, new_config)
        if _target(studio, course_id, chapter_name, section["section_title"]) != target:
            report["status"] = "unconfirmed"
            raise RepairError("target unit changed during update")
        if _files(studio, gallery_id) != files or _audio(studio, gallery_id) != new_audio or _config(studio, gallery_id) != new_config:
            raise RepairError("Gallery verification failed")
        report["phase"] = "publish"
        report["status"] = "unconfirmed"
        for _ in range(3):
            try:
                studio.publish_sequential(sequential_id)
            except Exception:
                pass
            published = studio.get_course_publication_structure(course_id).get(sequential_id, {})
            if published.get("published") is True and published.get("has_changes") is False:
                if _target(studio, course_id, chapter_name, section["section_title"]) != target or _files(studio, gallery_id) != files or _audio(studio, gallery_id) != new_audio or _config(studio, gallery_id) != new_config:
                    raise RepairError("Gallery changed after publication")
                break
            if published.get("published") not in (True, False) or published.get("has_changes") not in (True, False):
                raise RepairError("sequential publication state is unreadable")
            if _target(studio, course_id, chapter_name, section["section_title"]) != target or _files(studio, gallery_id) != files or _audio(studio, gallery_id) != new_audio or _config(studio, gallery_id) != new_config:
                raise RepairError("target changed while publication was pending")
        else:
            raise RepairError("sequential publication not confirmed")
        report.update({"status": "success", "phase": "complete", "image_match": "filename/count/order only"})
    except Exception as exc:
        report["reason"] = str(exc) if isinstance(exc, RepairError) else type(exc).__name__
        if gallery_id and old_audio and old_config and new_audio and report["phase"] == "update" and report["status"] != "unconfirmed":
            try:
                _replace(lambda: _config(studio, gallery_id), lambda value: studio.update_play_config(gallery_id, value), new_config, old_config)
                _replace(lambda: _audio(studio, gallery_id), lambda value: studio.bind_audio(gallery_id, value), new_audio, old_audio)
                report["recovery"] = "restored and verified"
            except Exception:
                report.update({"status": "unconfirmed", "recovery": "unconfirmed"})
    _write(output / "report.json", report)
    return report
