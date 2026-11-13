"""Upload one isolated image and reconcile its observed identity."""
from .repair_one import RepairError, _files


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
