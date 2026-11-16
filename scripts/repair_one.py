"""Single-section repair. Model, TTS and Studio are external boundaries."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from urllib.parse import urlparse

from .pagination import PaginationBoundaryError, validate_boundaries


class RepairError(Exception):
    pass


class RestoredRepairError(RepairError):
    pass


class UnconfirmedRepairError(RepairError):
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


def _retry_read(read):
    """Retry only read-only operations; never repeat an uncertain write."""
    for attempt in range(3):
        try:
            return read()
        except Exception:
            if attempt == 2:
                raise


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _input_digest(target_url, source_id, chapter, section, block, gallery, images, force_replace):
    payload = {"target_url": target_url, "source_id": source_id, "chapter": chapter,
               "section": section, "block": block, "gallery": gallery,
               "images": [{"path": str(image), "sha256": _sha256(image)} for image in images],
               "force_replace": force_replace}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _image_digests(images):
    return [{"path": str(image), "sha256": _sha256(image)} for image in images]


def _verify_isolated_mapping(output, state):
    if not state.get("isolated_images", state.get("replacement")):
        return
    try:
        mapping = _json(output / "image-map.json")
        prior_images = state["images"]
        files = state["files"]
        if not isinstance(mapping, list) or len(mapping) != len(prior_images) or len(mapping) != len(files):
            raise ValueError
        for row, image, remote in zip(mapping, prior_images, files):
            path = Path(row["isolated_file"]).resolve()
            if (row["source"] != image["path"] or not path.is_relative_to((output / "isolated-images").resolve())
                    or not path.is_file() or _sha256(path) != image["sha256"]
                    or row["asset_id"] != remote["id"] or path.name != remote["filename"]):
                raise ValueError
    except (KeyError, TypeError, OSError, ValueError) as exc:
        raise RepairError("isolated image mapping or artifacts changed") from exc


def _assert_no_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if any(word in normalized for word in ("password", "secret", "token", "cookie", "authorization", "accesskey")) and item:
                raise RepairError("Gallery configuration contains credential fields and cannot be checkpointed")
            _assert_no_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_credentials(item)


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(5):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05)


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


def _target(studio, course_id, chapter_name, section_title, gallery_locator=None):
    return _retry_read(lambda: _read_target(studio, course_id, chapter_name, section_title, gallery_locator))


def _read_target(studio, course_id, chapter_name, section_title, gallery_locator=None):
    root = studio.get_course_structure(course_id)
    chapter = _one([x for x in _children(root) if x.get("category") == "chapter" and x.get("display_name", "").strip() == chapter_name], "target chapter")
    sequential = _one([x for x in _children(chapter) if x.get("category") == "sequential" and x.get("display_name", "").strip() == section_title], "target sequential")
    vertical = _one([x for x in _children(sequential) if isinstance(x, dict) and x.get("category") == "vertical" and x.get("display_name", "").strip() == "赋能内容"], "target 赋能内容")
    locator = lambda node: node.get("id") or node.get("block_location") or node.get("locator")
    gallery = _one([x for x in _children(vertical) if x.get("category") == "imagesgallery" and (gallery_locator is None or locator(x) == gallery_locator)], "target Gallery")
    ids = [locator(sequential), locator(vertical), locator(gallery)]
    prefix = "block-v1:" + course_id.removeprefix("course-v1:") + "+type@"
    if any(not isinstance(locator, str) or not locator.startswith(prefix + kind + "+block@") or not locator.removeprefix(prefix + kind + "+block@") for locator, kind in zip(ids, ("sequential", "vertical", "imagesgallery"))):
        raise RepairError("target locators do not belong to the requested course")
    children = tuple((x.get("category"), locator(x)) for x in _children(vertical))
    if any(not category or not child_id for category, child_id in children):
        raise RepairError("target unit structure is incomplete")
    # Only the target vertical is published. Track its other components while
    # leaving sibling units outside the repair and publication boundary.
    other_components = [x for x in _children(vertical) if x.get("category") != "imagesgallery"]

    def component_label(kind, identity):
        category = kind if isinstance(kind, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", kind) else "unknown"
        pattern = re.escape(prefix) + r"[A-Za-z0-9_-]+\+block@[A-Za-z0-9_.~-]+"
        if isinstance(identity, str) and re.fullmatch(pattern, identity):
            return f"{category} ({identity})"
        return category

    def checked_children(node):
        if not isinstance(node, dict):
            raise UnconfirmedRepairError("the target unit structure is incomplete")
        kind = node.get("category")
        identity = locator(node)
        if kind not in {"vertical", "html", "problem", "imagesgallery"} or not identity:
            raise UnconfirmedRepairError(f"component cannot be checked: {component_label(kind, identity)}")
        info = node.get("child_info")
        has_children = node.get("has_children")
        if "has_children" in node and not isinstance(has_children, bool):
            raise UnconfirmedRepairError("the target unit structure is incomplete")
        if info is None:
            if has_children is True or (kind == "vertical" and has_children is not False):
                raise UnconfirmedRepairError(
                    f"the target unit structure is incomplete: child list unavailable for {component_label(kind, identity)}"
                )
        elif not isinstance(info, dict) or not isinstance(info.get("children"), list):
            raise UnconfirmedRepairError("the target unit structure is incomplete")
        elif has_children is False and info["children"]:
            raise UnconfirmedRepairError("the target unit structure is incomplete")
        return _children(node)

    def content(node):
        descendants = checked_children(node)
        kind = node.get("category")
        identity = locator(node)
        try:
            details = studio.get_block_details(identity)
        except Exception as exc:
            raise UnconfirmedRepairError("component content could not be read") from exc
        if (not isinstance(details, dict) or details.get("id") != identity or details.get("category") != kind
                or not isinstance(details.get("data"), str) or not isinstance(details.get("metadata"), dict)):
            raise UnconfirmedRepairError("component content response is incomplete")
        result = {"id": identity, "category": kind, "data": details["data"], "metadata": details["metadata"]}
        if kind == "imagesgallery":
            try:
                files = _files(studio, identity)
                audios = _retry_read(lambda: studio.get_audios(identity))
                config = _retry_read(lambda: studio.get_play_config(identity))
            except Exception as exc:
                raise UnconfirmedRepairError("other Gallery state is unreadable") from exc
            if (not isinstance(audios, dict) or not isinstance(audios.get("audios"), list)
                    or not isinstance(config, dict) or not isinstance(config.get("play_config"), dict)):
                raise UnconfirmedRepairError("other Gallery state is unreadable")
            result.update({"files": files, "audios": audios["audios"], "play_config": config["play_config"]})
        result["children"] = []
        for child in descendants:
            if node is vertical and isinstance(child, dict) and child.get("category") == "imagesgallery":
                gallery_children = checked_children(child)
                # The local ImageGallery contract is a leaf; a different or
                # incomplete shape cannot be checked using its read handlers.
                if child.get("has_children") is not False or gallery_children:
                    raise UnconfirmedRepairError("the target Gallery structure is incomplete")
            else:
                result["children"].append(content(child))
        return result

    snapshot = {"other_components": other_components, "content": content(vertical)}
    unit_snapshot = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")).encode("utf-8")).hexdigest()
    return *ids, children, unit_snapshot


def _target_from_saved(value):
    if not isinstance(value, (tuple, list)) or len(value) != 5 or not isinstance(value[4], str):
        raise RepairError("saved target unit snapshot is unavailable")
    return tuple(value[:3]) + (tuple(tuple(row) for row in value[3]), value[4])


def _files(studio, gallery_id):
    return _retry_read(lambda: _read_files(studio, gallery_id))


def _read_files(studio, gallery_id):
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
    return _retry_read(lambda: _read_audio(studio, gallery_id))


def _read_audio(studio, gallery_id):
    value = studio.get_audios(gallery_id).get("audios")
    if not isinstance(value, list) or len(value) != 1 or not value[0].get("document_id") or not value[0].get("file_name"):
        raise RepairError("Gallery audio identity is not recoverable")
    return {key: value[0][key] for key in ("document_id", "file_name")}


def _config(studio, gallery_id):
    return _retry_read(lambda: _read_config(studio, gallery_id))


def _read_config(studio, gallery_id):
    value = studio.get_play_config(gallery_id).get("play_config")
    if not isinstance(value, dict) or not isinstance(value.get("time_config"), list):
        raise RepairError("Gallery play config is unreadable")
    return value


def _publication(studio, course_id, sequential_id, vertical_id):
    def read():
        sections = studio.get_course_publication_structure(course_id)
        if not isinstance(sections, dict):
            raise UnconfirmedRepairError("parent sequential publication state is unreadable")
        section = sections.get(sequential_id, {})
        if not isinstance(section, dict) or section.get("published") is not True:
            raise UnconfirmedRepairError("parent sequential is not confirmed published")
        matches = [node for node in _children(section) if isinstance(node, dict)
                   and node.get("category") == "vertical"
                   and (node.get("id") or node.get("block_location") or node.get("locator")) == vertical_id]
        if (len(matches) != 1 or not isinstance(matches[0].get("published"), bool)
                or not isinstance(matches[0].get("has_changes"), bool)):
            raise UnconfirmedRepairError("target unit publication state is unreadable")
        return matches[0]
    return _retry_read(read)


def _verify_gallery(studio, course_id, chapter, section, target, files, audio, config, *, isolated=False):
    gallery = target[2]
    if (_target(studio, course_id, chapter, section) != target or _files(studio, gallery) != files
            or _audio(studio, gallery) != audio or _config(studio, gallery) != config):
        raise RepairError("Gallery or target unit changed before publication")
    if isolated:
        from .image_upload import check_candidate_metadata
        check_candidate_metadata(studio, gallery, expected_files=files, expected_audios=[audio], expected_config=config)


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


def repair_one(package, target_url, section_id, repair_dir, *, split, review, synthesize, studio, force_replace=False):
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
    replacing = False
    prior_complete = None
    try:
        section, source_id, chapter_name, block, gallery, images, voice = _retry_read(
            lambda: _load_inputs(package, section_id, repair_dir))
        report["source_course_id"] = source_id
        input_digest = _input_digest(target_url, source_id, chapter_name, section, block, gallery, images, force_replace)
        common = _nlm()
        source = common._clean_page_body(common._render_html(block["text"]))
        cover_title = common.strip_section_prefix(section["section_title"])
        body_parts = common._remove_duplicate_cover_heading([source], title=cover_title)
        body = body_parts[0] if body_parts else ""
        state_path = output / "run-state.json"
        if state_path.is_file():
            report["status"] = "unconfirmed"
            saved = _json(state_path)
            if not isinstance(saved, dict) or saved.get("stage") not in {"prepared", "audio_upload_pending", "audio_uploaded", "publish_pending", "complete"}:
                raise RepairError("saved run stage is unreadable")
            if saved.get("publication_scope") != "vertical":
                raise RepairError("saved run uses an earlier publication scope and requires reconciliation")
            saved_target = _target_from_saved(saved.get("target"))
            _publication(studio, course_id, saved_target[0], saved_target[1])
            if (saved.get("stage") == "prepared" and saved.get("input_digest") != input_digest
                    and not (output / "replacement-state.json").exists()):
                report["status"] = "unconfirmed"
                current = _target(studio, course_id, chapter_name, section["section_title"])
                if (json.loads(json.dumps(current)) != saved["target"] or _files(studio, current[2]) != saved["files"]
                        or _audio(studio, current[2]) != saved["old_audio"]
                        or _config(studio, current[2]) != saved["old_config"]):
                    raise RepairError("target changed since preparation")
                history = output / "history"
                history.mkdir(exist_ok=True)
                state_path.replace(history / f"{uuid.uuid4().hex}-run-state.json")
                saved = {}
                report["status"] = "failed"
            if saved.get("stage") in {"prepared", "audio_upload_pending", "audio_uploaded", "publish_pending"}:
                report.update({"status": "unconfirmed", "phase": "resume", "recovery": "unconfirmed"})
                if saved.get("replacement"):
                    report["replacement"] = True
                if saved.get("input_digest") != input_digest:
                    raise RepairError("input changed since preparation")
                manifest_path = output / "imagesgallery.json"
                saved_audio = Path(saved["audio_path"]).resolve()
                if (not saved_audio.is_relative_to((output / "audio").resolve()) or not saved_audio.is_file()
                        or not manifest_path.is_file() or _sha256(saved_audio) != saved["audio_sha256"]
                        or _sha256(manifest_path) != saved["manifest_sha256"]):
                    raise RepairError("artifacts changed since preparation")
                pages = common._normalize_explicit_page_content(saved["pages"])
                common._validate_html_pagination_source_coverage(body, pages[1:])
                if _validate_audio(manifest_path, pages, saved_audio) != saved["config"]["time_config"]:
                    raise RepairError("timeline differs from checked audio")
                if saved.get("replacement"):
                    if (output / "replacement-state.json").exists():
                        from .resume_replacement import resume_replacement
                        resumed_target, resumed_files, resumed_audio = resume_replacement(
                            studio, course_id, chapter_name, section["section_title"], output, images, saved)
                    else:
                        from .replacement import replace_gallery
                        old_target = _target_from_saved(saved["target"])
                        resumed_target, resumed_files, resumed_audio = replace_gallery(
                            studio, course_id, chapter_name, section["section_title"], old_target,
                            saved["files"], saved["old_audio"], saved["old_config"], saved["config"],
                            images, saved_audio, output)
                else:
                    from .resume_inplace import resume_inplace
                    resumed_target, resumed_files, resumed_audio = resume_inplace(
                        studio, course_id, chapter_name, section["section_title"], output, saved)
                sequential_id, vertical_id, gallery_id, children, _ = resumed_target
                report.update({"sequential": sequential_id, "vertical": vertical_id, "gallery": gallery_id})
                saved.update({"stage": "publish_pending", "audio": resumed_audio})
                _write(state_path, saved)
                for _ in range(3):
                    _verify_gallery(studio, course_id, chapter_name, section["section_title"], resumed_target,
                                    resumed_files, resumed_audio, saved["config"],
                                    isolated=saved.get("isolated_images", saved.get("replacement", False)))
                    publication = _publication(studio, course_id, sequential_id, vertical_id)
                    if publication.get("published") is True and publication.get("has_changes") is False:
                        break
                    try:
                        studio.publish_vertical(vertical_id)
                    except Exception:
                        pass
                else:
                    publication = _publication(studio, course_id, sequential_id, vertical_id)
                    if publication.get("published") is not True or publication.get("has_changes") is not False:
                        raise RepairError("target unit publication not confirmed")
                _verify_gallery(studio, course_id, chapter_name, section["section_title"], resumed_target,
                                resumed_files, resumed_audio, saved["config"],
                                isolated=saved.get("isolated_images", saved.get("replacement", False)))
                match_description = "isolated local uploads" if saved.get("isolated_images", saved.get("replacement")) else "filename/count/order only"
                saved.update({"stage": "complete", "target": resumed_target, "files": resumed_files,
                              "audio": resumed_audio, "image_match": match_description})
                _write(state_path, saved)
                report.update({"status": "success", "phase": "complete", "image_match": match_description,
                               "recovery": "resumed and verified"})
                _write(output / "report.json", report)
                return report
            if saved.get("stage") == "complete":
                report["status"] = "unconfirmed"
                _verify_isolated_mapping(output, saved)
            if saved.get("stage") == "complete" and saved.get("input_digest") != input_digest:
                report["status"] = "unconfirmed"
                current = _target(studio, course_id, chapter_name, section["section_title"])
                publication = _publication(studio, course_id, current[0], current[1])
                if (json.loads(json.dumps(current)) != saved["target"] or _files(studio, current[2]) != saved["files"]
                        or _audio(studio, current[2]) != saved["audio"] or _config(studio, current[2]) != saved["config"]
                        or publication.get("published") is not True or publication.get("has_changes") is not False):
                    raise RepairError("completed section changed since its verified snapshot")
                prior_complete = saved
                report["status"] = "failed"
            if saved.get("stage") == "complete" and saved.get("input_digest") == input_digest:
                manifest_path = output / "imagesgallery.json"
                saved_audio = Path(saved["audio_path"]).resolve()
                if (saved_audio.is_relative_to((output / "audio").resolve())
                        and manifest_path.is_file() and saved_audio.is_file()
                        and _sha256(manifest_path) == saved.get("manifest_sha256")
                        and _sha256(saved_audio) == saved.get("audio_sha256")):
                    pages = common._normalize_explicit_page_content(saved["pages"])
                    common._validate_html_pagination_source_coverage(body, pages[1:])
                    if _validate_audio(manifest_path, pages, saved_audio) != saved["config"]["time_config"]:
                        raise RepairError("saved timeline differs from checked audio")
                    current = _target(studio, course_id, chapter_name, section["section_title"])
                    publication = _publication(studio, course_id, current[0], current[1])
                    if (json.loads(json.dumps(current)) == saved["target"]
                            and _files(studio, current[2]) == saved["files"]
                            and _audio(studio, current[2]) == saved["audio"]
                            and _config(studio, current[2]) == saved["config"]
                            and publication.get("published") is True and publication.get("has_changes") is False):
                        _verify_gallery(studio, course_id, chapter_name, section["section_title"], current,
                                        saved["files"], saved["audio"], saved["config"],
                                        isolated=saved.get("isolated_images", saved.get("replacement", False)))
                        report.update({"status": "success", "phase": "complete", "sequential": current[0],
                                       "vertical": current[1], "gallery": current[2],
                                       "image_match": saved["image_match"], "reused": True})
                        if saved.get("replacement"):
                            report["replacement"] = True
                        _write(output / "report.json", report)
                        return report
                    old = _json(output / "old-state.json")
                    if (saved.get("replacement") or json.loads(json.dumps(current[3])) != old["children"]
                            or _files(studio, current[2]) != old["files"]
                            or _audio(studio, current[2]) != old["audio"]
                            or _config(studio, current[2]) != old["play_config"]):
                        report["status"] = "unconfirmed"
                        raise RepairError("completed section changed since its verified snapshot")
                    report["status"] = "failed"
                else:
                    report["status"] = "unconfirmed"
                    current = _target(studio, course_id, chapter_name, section["section_title"])
                    if (json.loads(json.dumps(current)) != saved["target"] or _files(studio, current[2]) != saved["files"]
                            or _audio(studio, current[2]) != saved["audio"] or _config(studio, current[2]) != saved["config"]):
                        raise RepairError("completed section changed since its verified snapshot")
                    prior_complete = saved
                    report["status"] = "failed"
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
                validate_boundaries(proposal)
                verdict = review({**copy.deepcopy(request), "pages": copy.deepcopy(proposal)})
                approved = verdict.get("approved") is True if isinstance(verdict, dict) else verdict is True
                if not approved:
                    reason = verdict.get("reason") if isinstance(verdict, dict) else None
                    request["previous_pages"] = copy.deepcopy(proposal)
                    request["review_feedback"] = (reason.strip()[:1200] if isinstance(reason, str) and reason.strip()
                                                  else "上一版未通过独立复核，请检查分页与图片主题。")
                    raise RepairError("independent review rejected pages")
                pages = proposal
                break
            except PaginationBoundaryError as exc:
                request["previous_pages"] = copy.deepcopy(proposal)
                request["review_feedback"] = str(exc)
                continue
            except Exception:
                continue
        if pages is None:
            raise RepairError("pagination or review did not pass")
        report["phase"] = "target"
        if (output / "replacement-state.json").exists():
            if not prior_complete:
                report["status"] = "unconfirmed"
                raise RepairError("an earlier Gallery replacement requires reconciliation before rerunning")
            history = output / "history"
            history.mkdir(exist_ok=True)
            nonce = uuid.uuid4().hex
            for name in ("replacement-state.json", "image-map.json"):
                old_path = output / name
                if old_path.exists():
                    if name == "image-map.json":
                        shutil.copyfile(old_path, history / f"{nonce}-{name}")
                    else:
                        old_path.replace(history / f"{nonce}-{name}")
        target = _target(studio, course_id, chapter_name, section["section_title"])
        sequential_id, vertical_id, gallery_id, children, _ = target
        _publication(studio, course_id, sequential_id, vertical_id)
        report.update({"sequential": sequential_id, "vertical": vertical_id, "gallery": gallery_id})
        files = _files(studio, gallery_id)
        same_previous_images = bool(prior_complete and prior_complete.get("images") == _image_digests(images))
        matching_replaced_images = bool(same_previous_images and prior_complete.get("isolated_images", prior_complete.get("replacement")) and files == prior_complete["files"])
        newly_forced = force_replace and not (matching_replaced_images and prior_complete.get("force_replace"))
        replacing = (newly_forced or (prior_complete is not None and not same_previous_images)
                     or (not matching_replaced_images and [x["filename"] for x in files] != [p.name for p in images]))
        old_audio, old_config = _audio(studio, gallery_id), _config(studio, gallery_id)
        _assert_no_credentials(old_config)
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
        _write(state_path, {"stage": "prepared", "publication_scope": "vertical", "input_digest": input_digest, "target": target,
                            "files": files, "old_audio": old_audio, "old_config": old_config,
                            "config": new_config, "pages": pages, "audio_path": str(Path(audio_path).resolve()),
                            "audio_sha256": _sha256(audio_path), "manifest_sha256": _sha256(manifest_path),
                            "replacement": replacing, "isolated_images": replacing or matching_replaced_images,
                            "images": _image_digests(images), "force_replace": force_replace})
        report["phase"] = "update"
        if replacing:
            from .replacement import replace_gallery
            report["status"] = "unconfirmed"
            target, files, new_audio = replace_gallery(
                studio, course_id, chapter_name, section["section_title"], target, files,
                old_audio, old_config, new_config, images, audio_path, output,
            )
            sequential_id, vertical_id, gallery_id, children, _ = target
            report.update({"gallery": gallery_id, "replacement": True})
        else:
            state = _json(state_path)
            state["stage"] = "audio_upload_pending"
            _write(state_path, state)
            try:
                new_audio = studio.upload_audio(gallery_id, audio_path)
            except Exception as exc:
                report["status"] = "unconfirmed"
                try:
                    _target(studio, course_id, chapter_name, section["section_title"])
                    _files(studio, gallery_id)
                    _audio(studio, gallery_id)
                    _config(studio, gallery_id)
                except Exception:
                    pass
                raise RepairError("audio upload outcome is unconfirmed") from exc
            if not isinstance(new_audio, dict) or not new_audio.get("document_id") or not new_audio.get("file_name") or new_audio == old_audio:
                report["status"] = "unconfirmed"
                raise RepairError("new audio identity is missing")
            state.update({"stage": "audio_uploaded", "audio": new_audio})
            _write(state_path, state)
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
        state = _json(state_path)
        state.update({"stage": "publish_pending", "audio": new_audio})
        _write(state_path, state)
        for _ in range(3):
            _verify_gallery(studio, course_id, chapter_name, section["section_title"], target,
                            files, new_audio, new_config, isolated=replacing or matching_replaced_images)
            try:
                studio.publish_vertical(vertical_id)
            except Exception:
                pass
            published = _publication(studio, course_id, sequential_id, vertical_id)
            if published.get("published") is True and published.get("has_changes") is False:
                _verify_gallery(studio, course_id, chapter_name, section["section_title"], target,
                                files, new_audio, new_config, isolated=replacing or matching_replaced_images)
                break
            if _target(studio, course_id, chapter_name, section["section_title"]) != target or _files(studio, gallery_id) != files or _audio(studio, gallery_id) != new_audio or _config(studio, gallery_id) != new_config:
                raise RepairError("target changed while publication was pending")
        else:
            raise RepairError("target unit publication not confirmed")
        report.update({"status": "success", "phase": "complete", "image_match": "isolated local uploads" if replacing or matching_replaced_images else "filename/count/order only"})
        _write(state_path, {"stage": "complete", "publication_scope": "vertical", "input_digest": input_digest, "target": target,
                            "files": files, "audio": new_audio, "config": new_config, "pages": pages,
                            "audio_path": str(Path(audio_path).resolve()), "audio_sha256": _sha256(audio_path),
                            "manifest_sha256": _sha256(manifest_path), "replacement": replacing,
                            "image_match": report["image_match"], "isolated_images": replacing or matching_replaced_images,
                            "images": _image_digests(images),
                            "force_replace": force_replace})
    except Exception as exc:
        report["reason"] = str(exc) if isinstance(exc, RepairError) else type(exc).__name__
        if isinstance(exc, UnconfirmedRepairError):
            report["status"] = "unconfirmed"
        if isinstance(exc, RestoredRepairError):
            report.update({"status": "failed", "recovery": "restored and verified"})
        if replacing:
            report.update({"replacement": True, "recovery": "not attempted"})
            state_path = output / "replacement-state.json"
            if state_path.is_file():
                try:
                    state = _json(state_path)
                    report["replacement_stage"] = state.get("stage")
                    if state.get("new_gallery"):
                        report["candidate_gallery"] = state["new_gallery"]
                except (OSError, ValueError, AttributeError):
                    report["replacement_stage"] = "unreadable"
        if not replacing and gallery_id and old_audio and old_config and new_audio and report["phase"] == "update" and report["status"] != "unconfirmed":
            try:
                _replace(lambda: _config(studio, gallery_id), lambda value: studio.update_play_config(gallery_id, value), new_config, old_config)
                _replace(lambda: _audio(studio, gallery_id), lambda value: studio.bind_audio(gallery_id, value), new_audio, old_audio)
                report["recovery"] = "restored and verified"
            except Exception:
                report.update({"status": "unconfirmed", "recovery": "unconfirmed"})
    _write(output / "report.json", report)
    return report
