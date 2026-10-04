"""Validate and snapshot supported image files for request-only use."""

import hashlib

from squid_core.image_content import (
    MAX_IMAGE_BYTES,
    ImageValidationError,
    inspect_image_bytes,
)

from .context import FileReadContext


TOOL_NAME = "read_file"


def read_image(context: FileReadContext) -> dict:
    """Validate and snapshot an image while preserving its exact source bytes."""

    if context.ranges is not None:
        return _image_error(
            context.path,
            "line ranges are not supported for image files",
        )
    if context.snapshot_asset is None:
        return _image_error(
            context.path,
            "image reads require active session asset storage",
        )

    try:
        size_bytes = context.file_path.stat().st_size
        if size_bytes > MAX_IMAGE_BYTES:
            raise ImageValidationError(
                f"image is {size_bytes} bytes; maximum supported size is "
                f"{MAX_IMAGE_BYTES} bytes"
            )
        data = context.file_path.read_bytes()
        metadata = inspect_image_bytes(data, context.file_path.suffix)
    except (ImageValidationError, OSError) as error:
        return _image_error(context.path, str(error))

    digest = hashlib.sha256(data).hexdigest()
    try:
        asset_id = context.snapshot_asset(data, metadata["canonical_extension"])
        expected_asset_id = f"{digest}{metadata['canonical_extension']}"
        if asset_id != expected_asset_id:
            raise OSError(
                "session asset storage returned an unexpected content identifier"
            )
    except Exception as error:
        return _image_error(
            context.path,
            f"could not snapshot image into the session: {error}",
        )

    return {
        "ok": True,
        "tool": TOOL_NAME,
        "path": context.path,
        "media_type": "image",
        "image": {
            "version": 1,
            "asset_id": asset_id,
            "sha256": digest,
            "mime_type": metadata["mime_type"],
            "format": metadata["format"],
            "width": metadata["width"],
            "height": metadata["height"],
            "size_bytes": metadata["size_bytes"],
        },
    }


def _image_error(path: str, error: str) -> dict:
    return {"ok": False, "tool": TOOL_NAME, "path": path, "error": error}
