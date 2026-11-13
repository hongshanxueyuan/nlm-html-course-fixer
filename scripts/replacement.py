"""Prepare and verify a replacement Gallery before removing its predecessor."""
from __future__ import annotations

import copy
from pathlib import Path
import shutil
import uuid

from .repair_one import RepairError, _audio, _config, _files, _replace, _target, _write


def replace_gallery(studio, course_id, chapter, section, target, old_files, old_audio, old_config, new_config, images, audio_path, output):
    sequential, vertical, old_gallery, baseline = target
    state_path = output / "replacement-state.json"
    image_map_path = output / "image-map.json"
    copies = output / "isolated-images"
    copies.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex
    mapping = []
    for index, source in enumerate(images, 1):
        destination = copies / f"repair-{nonce}-{index:03d}{source.suffix.lower()}"
        shutil.copyfile(source, destination)
        mapping.append({"source": str(source), "isolated_file": str(destination), "asset_id": None})
    _write(image_map_path, mapping)
    if (_target(studio, course_id, chapter, section) != target or _files(studio, old_gallery) != old_files
            or _audio(studio, old_gallery) != old_audio or _config(studio, old_gallery) != old_config):
        raise RepairError("target changed before Gallery creation")
    state = {"stage": "create_pending", "course_id": course_id, "vertical": vertical,
             "old_gallery": old_gallery, "original_children": baseline}
    _write(state_path, state)

    def original_target():
        return _target(studio, course_id, chapter, section, old_gallery)

    # Even when create times out, the actual structure is the authority.
    returned = None
    try:
        returned = studio.create_gallery(vertical)
    except Exception:
        pass
    current = original_target()
    current_children = current[3]
    additions = [child for child in current_children if child not in baseline]
    if (current[:3] != target[:3] or len(additions) != 1 or additions[0][0] != "imagesgallery"
            or tuple(child for child in current_children if child != additions[0]) != baseline
            or (returned and returned != additions[0][1])):
        raise RepairError("new Gallery creation could not be reconciled")
    new_gallery = additions[0][1]
    created_target = _target(studio, course_id, chapter, section, new_gallery)
    if created_target[:2] != target[:2] or created_target[3] != current_children:
        raise RepairError("new Gallery identity could not be confirmed")
    state.update({"stage": "created", "new_gallery": new_gallery, "created_children": current_children})
    _write(state_path, state)

    def check_original(expected_children):
        current = original_target()
        if (current[:3] != target[:3] or current[3] != expected_children
                or _files(studio, old_gallery) != old_files or _audio(studio, old_gallery) != old_audio
                or _config(studio, old_gallery) != old_config):
            raise RepairError("original Gallery or unit changed during replacement")

    check_original(current_children)
    uploaded = _files(studio, new_gallery)
    if uploaded:
        raise RepairError("new Gallery was not empty")
    for row in mapping:
        path = Path(row["isolated_file"])
        asset = None
        state.update({"stage": "upload_pending", "pending_file": path.name})
        _write(state_path, state)
        try:
            asset = studio.upload_image(new_gallery, path)
        except Exception:
            pass
        observed = _files(studio, new_gallery)
        additions = [item for item in observed if item not in uploaded]
        if (len(additions) != 1 or additions[0]["filename"] != path.name
                or [item for item in observed if item != additions[0]] != uploaded
                or (asset and str(asset) != additions[0]["id"])):
            raise RepairError("image upload could not be reconciled")
        row["asset_id"] = additions[0]["id"]
        uploaded = observed
        _write(image_map_path, mapping)
        state.update({"stage": "uploading", "pending_file": None})
        _write(state_path, state)
        check_original(current_children)

    desired_files = [{"id": row["asset_id"], "filename": Path(row["isolated_file"]).name} for row in mapping]
    if uploaded != desired_files:
        _replace(lambda: _files(studio, new_gallery),
                 lambda value: studio.order_images(new_gallery, [item["id"] for item in value]),
                 uploaded, desired_files)
    check_original(current_children)

    state["stage"] = "audio_upload_pending"
    _write(state_path, state)
    try:
        new_audio = studio.upload_audio(new_gallery, audio_path)
        if not isinstance(new_audio, dict) or not new_audio.get("document_id") or not new_audio.get("file_name"):
            raise ValueError("new audio identity is missing")
    except Exception as exc:
        # Registration can succeed without yielding its identity; read the Gallery,
        # but do not repeat an upload whose document identity cannot be recovered.
        try:
            studio.get_audios(new_gallery)
            _files(studio, new_gallery)
            check_original(current_children)
        except Exception:
            pass
        raise RepairError("audio upload outcome is unconfirmed") from exc

    def bindings():
        payload = studio.get_audios(new_gallery)
        rows = payload.get("audios") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RepairError("new Gallery audio is unreadable")
        return rows

    if bindings() != []:
        raise RepairError("new Gallery unexpectedly has audio")
    _replace(bindings, lambda value: studio.bind_audio(new_gallery, value[0]), [], [new_audio])
    def new_gallery_config():
        payload = studio.get_play_config(new_gallery)
        value = payload.get("play_config") if isinstance(payload, dict) else None
        if not isinstance(value, dict):
            raise RepairError("new Gallery play config is unreadable")
        return value

    initial_config = new_gallery_config()
    _replace(new_gallery_config, lambda value: studio.update_play_config(new_gallery, value), initial_config, new_config)
    check_original(current_children)
    if _files(studio, new_gallery) != desired_files or _audio(studio, new_gallery) != new_audio or _config(studio, new_gallery) != new_config:
        raise RepairError("new Gallery contents did not pass readback")
    state.update({"stage": "verified", "audio": new_audio})
    _write(state_path, state)

    # Keep the old Gallery until the complete new Gallery occupies its position.
    moved = list(baseline)
    moved.insert(moved.index(("imagesgallery", old_gallery)), ("imagesgallery", new_gallery))
    moved = tuple(moved)
    def children():
        observed = _target(studio, course_id, chapter, section, new_gallery)
        if observed[:2] != target[:2]:
            raise RepairError("target unit identity changed")
        return observed[3]

    state["stage"] = "reorder_pending"
    _write(state_path, state)
    _replace(children, lambda value: studio.reorder_children(vertical, [identity for _, identity in value]), current_children, moved)
    check_original(moved)
    if _files(studio, new_gallery) != desired_files or _audio(studio, new_gallery) != new_audio or _config(studio, new_gallery) != new_config:
        raise RepairError("new Gallery changed before replacement")
    final_children = tuple(child for child in moved if child != ("imagesgallery", old_gallery))
    state["stage"] = "delete_pending"
    _write(state_path, state)
    _replace(children, lambda value: studio.delete_gallery(old_gallery), moved, final_children)
    final_target = _target(studio, course_id, chapter, section)
    if final_target[:2] != (sequential, vertical) or final_target[2] != new_gallery or final_target[3] != final_children:
        raise RepairError("replacement structure could not be confirmed")
    if _files(studio, new_gallery) != desired_files or _audio(studio, new_gallery) != new_audio or _config(studio, new_gallery) != new_config:
        raise RepairError("replacement Gallery changed after deletion")
    state["stage"] = "replaced"
    _write(state_path, state)
    return final_target, desired_files, new_audio
