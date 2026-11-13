"""Reconcile a persisted replacement with the current Gallery before continuing."""
from pathlib import Path

from .repair_one import RepairError, _audio, _config, _files, _json, _replace, _retry_read, _sha256, _target, _target_from_saved, _write
from .image_upload import upload_image


def resume_replacement(studio, course_id, chapter, section, output, images, prepared):
    state_path = output / "replacement-state.json"
    state = _json(state_path)
    stages = {"create_pending", "created", "upload_pending", "uploading", "audio_upload_pending",
              "audio_uploaded", "verified", "reorder_pending", "delete_pending", "replaced"}
    if state.get("course_id") != course_id or state.get("stage") not in stages:
        raise RepairError("replacement stage cannot be safely reconciled")
    old_target = _target_from_saved(prepared["target"])
    sequential, vertical, old_gallery = old_target[:3]
    baseline = tuple(tuple(row) for row in old_target[3])
    new_gallery = state.get("new_gallery")
    if state.get("vertical") != vertical or state.get("old_gallery") != old_gallery:
        raise RepairError("replacement identity changed")
    mapping = _json(output / "image-map.json")
    if not isinstance(mapping, list) or len(mapping) != len(images):
        raise RepairError("replacement image mapping is incomplete")
    for source, row in zip(images, mapping):
        path = Path(row.get("isolated_file", "")).resolve()
        if (row.get("source") != str(source) or not path.is_relative_to((output / "isolated-images").resolve())
                or not path.is_file() or _sha256(path) != _sha256(source)):
            raise RepairError("replacement image mapping changed")
    if state["stage"] == "create_pending":
        current = _target(studio, course_id, chapter, section, old_gallery)
        additions = [child for child in current[3] if child not in baseline]
        if (not isinstance(new_gallery, str) or not new_gallery or not isinstance(state.get("initial_config"), dict)
                or current[:3] != (sequential, vertical, old_gallery) or current[4] != old_target[4] or len(additions) != 1
                or additions[0][0] != "imagesgallery" or tuple(child for child in current[3] if child != additions[0]) != baseline):
            raise RepairError("new Gallery creation remains unconfirmed")
        if additions[0][1] != new_gallery:
            raise RepairError("new Gallery creation remains unconfirmed")
        state.update({"stage": "created", "new_gallery": new_gallery, "created_children": current[3]})
        _write(state_path, state)
    if not new_gallery or not isinstance(state.get("initial_config"), dict):
        raise RepairError("replacement Gallery identity is unavailable")
    created = tuple(tuple(row) for row in state.get("created_children", []))
    moved = list(baseline)
    moved.insert(moved.index(("imagesgallery", old_gallery)), ("imagesgallery", new_gallery))
    moved = tuple(moved)
    final = tuple(child for child in moved if child != ("imagesgallery", old_gallery))

    def read_children():
        current = _target(studio, course_id, chapter, section, new_gallery)
        if current[:2] != (sequential, vertical) or current[4] != old_target[4]:
            raise RepairError("replacement unit changed")
        return current[3]

    def verify_new():
        if (_files(studio, new_gallery) != expected_files or _audio(studio, new_gallery) != new_audio
                or _config(studio, new_gallery) != prepared["config"]):
            raise RepairError("replacement Gallery changed")

    def verify_old():
        if (_files(studio, old_gallery) != prepared["files"] or _audio(studio, old_gallery) != prepared["old_audio"]
                or _config(studio, old_gallery) != prepared["old_config"]):
            raise RepairError("original Gallery changed")

    if state["stage"] in {"created", "upload_pending", "uploading"}:
        current_old = _target(studio, course_id, chapter, section, old_gallery)
        if current_old[3] != created or current_old[4] != old_target[4]:
            raise RepairError("replacement unit changed before image upload")
        verify_old()
        uploaded = _files(studio, new_gallery)
        known_ids = {row["asset_id"] for row in mapping if row.get("asset_id")}
        if len(known_ids) != sum(bool(row.get("asset_id")) for row in mapping):
            raise RepairError("replacement image identities are ambiguous")
        extras = [row for row in uploaded if row["id"] not in known_ids]
        if state["stage"] == "upload_pending":
            pending = [row for row in mapping if Path(row["isolated_file"]).name == state.get("pending_file")]
            if len(pending) != 1:
                raise RepairError("image upload outcome is unconfirmed")
            if pending[0].get("asset_id"):
                if extras or {"id": pending[0]["asset_id"], "filename": state["pending_file"]} not in uploaded:
                    raise RepairError("image upload outcome is unconfirmed")
            else:
                if len(extras) != 1 or extras[0]["filename"] != state.get("pending_file"):
                    raise RepairError("image upload outcome is unconfirmed")
                pending[0]["asset_id"] = extras[0]["id"]
            _write(output / "image-map.json", mapping)
            state.update({"stage": "uploading", "pending_file": None})
            _write(state_path, state)
        elif extras:
            raise RepairError("new Gallery contains unexpected images")
        expected_known = {(row["asset_id"], Path(row["isolated_file"]).name) for row in mapping if row.get("asset_id")}
        if {(row["id"], row["filename"]) for row in uploaded} != expected_known:
            raise RepairError("uploaded images do not match the saved mapping")
        for row in mapping:
            if row.get("asset_id"):
                continue
            current_old = _target(studio, course_id, chapter, section, old_gallery)
            if current_old[3] != created or current_old[4] != old_target[4]:
                raise RepairError("replacement unit changed during image upload")
            verify_old()
            path = Path(row["isolated_file"])
            state.update({"stage": "upload_pending", "pending_file": path.name})
            _write(state_path, state)
            row["asset_id"], uploaded = upload_image(studio, new_gallery, path, uploaded)
            _write(output / "image-map.json", mapping)
            state.update({"stage": "uploading", "pending_file": None})
            _write(state_path, state)
        expected_files = [{"id": row["asset_id"], "filename": Path(row["isolated_file"]).name} for row in mapping]
        if uploaded != expected_files:
            _replace(lambda: _files(studio, new_gallery), lambda value: studio.order_images(new_gallery, [item["id"] for item in value]), uploaded, expected_files)
        verify_old()
        state["stage"] = "audio_upload_pending"
        _write(state_path, state)
        try:
            new_audio = studio.upload_audio(new_gallery, prepared["audio_path"])
        except Exception as exc:
            studio.get_audios(new_gallery)
            raise RepairError("audio upload outcome is unconfirmed") from exc
        if not isinstance(new_audio, dict) or not new_audio.get("document_id") or not new_audio.get("file_name"):
            raise RepairError("replacement audio identity is unavailable")
        state.update({"stage": "audio_uploaded", "audio": new_audio})
        _write(state_path, state)
    if state["stage"] == "audio_upload_pending":
        studio.get_audios(new_gallery)
        raise RepairError("audio upload outcome is unconfirmed")
    expected_files = [{"id": row.get("asset_id"), "filename": Path(row["isolated_file"]).name} for row in mapping]
    if any(not item["id"] for item in expected_files):
        raise RepairError("replacement image mapping is incomplete")
    new_audio = state.get("audio")
    if not isinstance(new_audio, dict) or not new_audio.get("document_id") or not new_audio.get("file_name"):
        raise RepairError("replacement audio identity is unavailable")
    if state["stage"] == "audio_uploaded":
        if read_children() != created:
            raise RepairError("replacement unit changed before binding audio")
        verify_old()
        if _files(studio, new_gallery) != expected_files:
            raise RepairError("replacement images changed")
        def bindings():
            payload = _retry_read(lambda: studio.get_audios(new_gallery))
            rows = payload.get("audios") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise RepairError("new Gallery audio is unreadable")
            return rows
        _replace(bindings, lambda value: studio.bind_audio(new_gallery, value[0]), [], [new_audio])
        def read_config():
            value = _retry_read(lambda: studio.get_play_config(new_gallery)).get("play_config")
            if not isinstance(value, dict):
                raise RepairError("new Gallery config is unreadable")
            return value
        _replace(read_config, lambda value: studio.update_play_config(new_gallery, value), state["initial_config"], prepared["config"])
        verify_old()
        verify_new()
        state["stage"] = "verified"
        _write(state_path, state)

    verify_new()
    current = read_children()
    if current in (created, moved):
        verify_old()
    if current == created:
        state["stage"] = "reorder_pending"
        _write(state_path, state)
        _replace(read_children, lambda value: studio.reorder_children(vertical, [identity for _, identity in value]), created, moved)
        current = read_children()
    if current == moved:
        verify_old()
        verify_new()
        state["stage"] = "delete_pending"
        _write(state_path, state)
        _replace(read_children, lambda value: studio.delete_gallery(old_gallery), moved, final)
        current = read_children()
    if current != final:
        raise RepairError("replacement structure changed")
    verify_new()
    target = _target(studio, course_id, chapter, section)
    if target[:3] != (sequential, vertical, new_gallery) or target[3] != final or target[4] != old_target[4]:
        raise RepairError("replacement structure could not be confirmed")
    state["stage"] = "replaced"
    _write(state_path, state)
    return target, expected_files, new_audio
