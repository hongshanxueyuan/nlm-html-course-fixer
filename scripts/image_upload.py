"""Upload one isolated image and reconcile its observed identity."""
from .repair_one import RepairError, _files, _retry_read


def check_candidate_metadata(studio, gallery, *, expected_files=None, expected_audios=None, expected_config=None):
    details = _retry_read(lambda: studio.get_block_details(gallery))
    allowed = {"contents", "content_names", "audio_contents", "play_config"}
    if (not isinstance(details, dict) or details.get("id") != gallery
            or details.get("category") != "imagesgallery" or details.get("data") != ""
            or not isinstance(details.get("metadata"), dict)
            or set(details["metadata"]) - allowed):
        raise RepairError("new Gallery has unexpected metadata")
    files = _retry_read(lambda: studio.get_files(gallery))
    audios = _retry_read(lambda: studio.get_audios(gallery))
    config = _retry_read(lambda: studio.get_play_config(gallery))
    if (not isinstance(files, dict) or not isinstance(files.get("files"), list)
            or isinstance(files.get("total_count"), bool) or files.get("total_count") != len(files["files"])
            or not isinstance(audios, dict) or not isinstance(audios.get("audios"), list)
            or not isinstance(config, dict) or not isinstance(config.get("play_config"), dict)):
        raise RepairError("new Gallery metadata cannot be reconciled")
    names, normalized = [], []
    for row in files["files"]:
        if not isinstance(row, dict):
            raise RepairError("new Gallery metadata cannot be reconciled")
        name = row.get("display_name") or row.get("name") or row.get("filename") or row.get("file_name")
        if not isinstance(name, str) or not name:
            raise RepairError("new Gallery metadata cannot be reconciled")
        names.append(name)
        identity = str(row.get("id") or row.get("asset_key") or "").strip()
        normalized.append({"id": identity, "filename": name.replace("\\", "/").rsplit("/", 1)[-1]})
    metadata = details["metadata"]
    if (metadata.get("contents", []) != files["files"] or metadata.get("content_names", []) != names
            or metadata.get("audio_contents", []) != audios["audios"]
            or metadata.get("play_config", {}) != config["play_config"]):
        raise RepairError("new Gallery metadata differs from readback")
    if ((expected_files is not None and normalized != expected_files)
            or (expected_audios is not None and audios["audios"] != expected_audios)
            or (expected_config is not None and config["play_config"] != expected_config)):
        raise RepairError("new Gallery differs from the expected repair stage")
    return metadata


def confirm_empty_gallery(studio, gallery):
    metadata = check_candidate_metadata(studio, gallery)
    config = _retry_read(lambda: studio.get_play_config(gallery))
    audios = _retry_read(lambda: studio.get_audios(gallery))
    if (metadata != {} or not isinstance(config, dict) or config.get("play_config") != {}
            or not isinstance(audios, dict) or audios.get("audios") != []
            or _files(studio, gallery) != []):
        raise RepairError("new Gallery does not match the confirmed empty default")
    return {}


def upload_image(studio, gallery, path, previous):
    returned = None
    try:
        returned = studio.upload_image(gallery, path)
    except Exception:
        pass
    observed = _files(studio, gallery)
    additions = [item for item in observed if item not in previous]
    if (len(additions) != 1 or additions[0]["filename"] != path.name
            or [item for item in observed if item != additions[0]] != previous
            or (returned and str(returned) != additions[0]["id"])):
        raise RepairError("image upload outcome is unconfirmed")
    return additions[0]["id"], observed
