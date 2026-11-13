"""Reconcile an in-place update after interruption."""
from .repair_one import RepairError, RestoredRepairError, _audio, _config, _files, _replace, _target, _target_from_saved, _write


def resume_inplace(studio, course_id, chapter, section, output, state):
    target = _target_from_saved(state["target"])
    gallery = target[2]

    def verify_structure():
        if _target(studio, course_id, chapter, section) != target or _files(studio, gallery) != state["files"]:
            raise RepairError("target changed during in-place recovery")

    verify_structure()
    if state["stage"] == "audio_upload_pending":
        # Registration may have completed without returning a document identity.
        _audio(studio, gallery)
        _config(studio, gallery)
        raise RepairError("audio upload outcome is unconfirmed")
    if state["stage"] == "prepared":
        if _audio(studio, gallery) != state["old_audio"] or _config(studio, gallery) != state["old_config"]:
            raise RepairError("target changed before audio upload")
        state["stage"] = "audio_upload_pending"
        _write(output / "run-state.json", state)
        try:
            audio = studio.upload_audio(gallery, state["audio_path"])
        except Exception as exc:
            _audio(studio, gallery)
            _config(studio, gallery)
            raise RepairError("audio upload outcome is unconfirmed") from exc
        if not isinstance(audio, dict) or not audio.get("document_id") or not audio.get("file_name") or audio == state["old_audio"]:
            raise RepairError("new audio identity is unavailable")
        state.update({"stage": "audio_uploaded", "audio": audio})
        _write(output / "run-state.json", state)
    audio = state.get("audio")
    if not isinstance(audio, dict) or not audio.get("document_id") or not audio.get("file_name"):
        raise RepairError("new audio identity is unavailable")
    try:
        verify_structure()
        _replace(lambda: _audio(studio, gallery), lambda value: studio.bind_audio(gallery, value), state["old_audio"], audio)
        verify_structure()
        _replace(lambda: _config(studio, gallery), lambda value: studio.update_play_config(gallery, value), state["old_config"], state["config"])
        verify_structure()
        if _audio(studio, gallery) != audio or _config(studio, gallery) != state["config"]:
            raise RepairError("in-place recovery could not be confirmed")
    except Exception as exc:
        try:
            verify_structure()
            _replace(lambda: _config(studio, gallery), lambda value: studio.update_play_config(gallery, value), state["config"], state["old_config"])
            _replace(lambda: _audio(studio, gallery), lambda value: studio.bind_audio(gallery, value), audio, state["old_audio"])
        except Exception as restore_exc:
            raise RepairError("in-place recovery is unconfirmed") from restore_exc
        raise RestoredRepairError("in-place update failed") from exc
    return target, state["files"], audio
