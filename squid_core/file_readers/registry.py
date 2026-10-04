"""Select a built-in file reader from a deliberately small suffix registry."""

from typing import Callable

from squid_core.image_content import SUPPORTED_IMAGE_SUFFIXES

from .context import FileReadContext
from .docx import read_docx
from .epub import read_epub
from .image import read_image
from .ods import read_ods
from .pdf import read_pdf
from .text import read_text
from .xlsx import read_xlsx


Reader = Callable[[FileReadContext], dict]

# Unknown suffixes intentionally retain the historical UTF-8 text fallback.
_READERS_BY_SUFFIX: dict[str, Reader] = {
    ".docx": read_docx,
    ".epub": read_epub,
    ".ods": read_ods,
    ".pdf": read_pdf,
    ".xlsx": read_xlsx,
    **{suffix: read_image for suffix in SUPPORTED_IMAGE_SUFFIXES},
}


def reader_for_suffix(suffix: str) -> Reader:
    """Return the registered reader, falling back to the ordinary text reader."""

    return _READERS_BY_SUFFIX.get(suffix.lower(), read_text)


def read_with_registered_reader(
    context: FileReadContext,
) -> dict:
    """Invoke the reader registered for this validated request."""

    reader = reader_for_suffix(context.file_path.suffix)
    return reader(context)
