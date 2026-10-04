"""Validate local images and describe request-only session asset references."""

from __future__ import annotations

import warnings
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError


MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
SUPPORTED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
VISION_UNAVAILABLE_MESSAGE = (
    "Vision not available for current model. "
    "Use an AI model with vision capability to view images."
)
# GIF is deliberately recognized only to produce a useful unsupported-media
# error instead of falling through to a confusing UTF-8 decoding failure.
UNSUPPORTED_IMAGE_SUFFIXES = frozenset({".gif"})

_FORMAT_DETAILS = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "WEBP": ("image/webp", ".webp"),
}
_SUFFIX_FORMATS = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
}


class ImageValidationError(ValueError):
    """Report an image that is unsupported, corrupt, or unsafe to decode."""


def inspect_image_bytes(data: bytes, source_suffix: str) -> dict[str, Any]:
    """Validate image bytes and return provider-neutral immutable metadata."""

    if len(data) > MAX_IMAGE_BYTES:
        raise ImageValidationError(
            f"image is {len(data)} bytes; maximum supported size is {MAX_IMAGE_BYTES} bytes"
        )

    suffix = source_suffix.lower()
    expected_format = _SUFFIX_FORMATS.get(suffix)
    if expected_format is None:
        raise ImageValidationError(f"unsupported image extension: {source_suffix}")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                image_format = (image.format or "").upper()
                width, height = image.size
                if image_format not in _FORMAT_DETAILS:
                    raise ImageValidationError(
                        f"unsupported image format: {image_format or 'unknown'}"
                    )
                if image_format != expected_format:
                    raise ImageValidationError(
                        f"image extension {suffix} does not match detected {image_format} format"
                    )
                if width < 1 or height < 1:
                    raise ImageValidationError("image dimensions must be positive")
                pixel_count = width * height
                if pixel_count > MAX_IMAGE_PIXELS:
                    raise ImageValidationError(
                        f"image has {pixel_count} pixels; maximum supported count is "
                        f"{MAX_IMAGE_PIXELS}"
                    )
                # Verification inspects the original stream without re-encoding it.
                image.verify()
    except ImageValidationError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as error:
        raise ImageValidationError(f"invalid or corrupt image: {error}") from error

    mime_type, canonical_extension = _FORMAT_DETAILS[image_format]
    return {
        "format": image_format,
        "mime_type": mime_type,
        "canonical_extension": canonical_extension,
        "width": width,
        "height": height,
        "size_bytes": len(data),
    }


def image_descriptor(result: object) -> dict[str, Any] | None:
    """Return a validated canonical image descriptor from a tool result."""

    if not isinstance(result, dict) or result.get("media_type") != "image":
        return None
    image = result.get("image")
    if not isinstance(image, dict) or image.get("version") != 1:
        return None

    asset_id = image.get("asset_id")
    sha256 = image.get("sha256")
    image_format = image.get("format")
    mime_type = image.get("mime_type")
    width = image.get("width")
    height = image.get("height")
    size_bytes = image.get("size_bytes")
    if (
        not isinstance(asset_id, str)
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
        or image_format not in _FORMAT_DETAILS
        or mime_type != _FORMAT_DETAILS[image_format][0]
        or asset_id != f"{sha256}{_FORMAT_DETAILS[image_format][1]}"
        or not all(
            isinstance(value, int) and not isinstance(value, bool) and value > 0
            for value in (width, height, size_bytes)
        )
    ):
        return None
    return image


def image_marker(path: object, image: dict[str, Any]) -> str:
    """Render compact, printable image metadata without image payload bytes."""

    filename = Path(str(path or "image")).name
    size = _format_byte_size(image["size_bytes"])
    return (
        f"[image: {filename}, {image['width']}x{image['height']} "
        f"{image['format']}, {size}, asset={image['asset_id']}]"
    )


def image_reference_block(result: dict[str, Any]) -> dict[str, Any] | None:
    """Build the narrow request-only block consumed by provider adapters."""

    image = image_descriptor(result)
    if image is None or result.get("ok") is not True:
        return None
    return {
        "type": "image_asset",
        "asset_id": image["asset_id"],
        "mime_type": image["mime_type"],
        "description": image_marker(result.get("path"), image),
    }


def _format_byte_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KiB"
    return f"{size_bytes / (1024 * 1024):.1f} MiB"
