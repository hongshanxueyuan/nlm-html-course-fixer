"""Upload one isolated image and reconcile its observed identity."""
from .repair_one import RepairError, _files, _retry_read


def check_candidate_metadata(studio, gallery):
    details = _retry_read(lambda: studio.get_block_details(gallery))
    allowed = {"contents", "content_names", "audio_contents", "play_config"}
    if (not isinstance(details, dict) or details.get("id") != gallery
            or details.get("category") != "imagesgallery" or details.get("data") != ""
            or not isinstance(details.get("metadata"), dict)
            or set(details["metadata"]) - allowed):
        raise RepairError("new Gallery has unexpected metadata")
    return details["metadata"]


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
