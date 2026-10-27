"""Private file storage for documents.

Files go to settings.STORAGES["documents"], which by default is a FileSystemStorage rooted at
PRIVATE_MEDIA_ROOT, a folder that is never served by Django, WhiteNoise or the web server.
Point it at a private object-storage bucket in production. Keys are random and say nothing
about the file or its owner.

Uploads are checked against an allow-list of file types by content (magic bytes), not just the
name the browser sent.
"""

import hashlib
import uuid
import zipfile
from dataclasses import dataclass

from django.conf import settings
from django.core.files.base import File
from django.core.files.storage import storages

# extension -> mime type. The contents are checked against the extension in _looks_like.
ALLOWED = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "csv": "text/csv",
    "txt": "text/plain",
}
OOXML_PREFIX = {"docx": "word/", "xlsx": "xl/", "pptx": "ppt/"}


class UploadRejected(ValueError):
    pass


@dataclass(frozen=True)
class StoredFile:
    storage_key: str
    mime_type: str
    size: int
    checksum: str
    extension: str


def max_upload_bytes():
    return getattr(settings, "DOCUMENT_MAX_UPLOAD_BYTES", 10 * 1024 * 1024)


def storage():
    return storages["documents"]


def _extension(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _looks_like(ext, head, upload):
    if ext == "pdf":
        return head.startswith(b"%PDF-")
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in ("jpg", "jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext in OOXML_PREFIX:
        if not head.startswith(b"PK\x03\x04"):
            return False
        upload.seek(0)
        try:
            with zipfile.ZipFile(upload) as archive:
                names = archive.namelist()
        except zipfile.BadZipFile:
            return False
        return "[Content_Types].xml" in names and any(n.startswith(OOXML_PREFIX[ext]) for n in names)
    if ext in ("csv", "txt"):
        if b"\x00" in head:
            return False
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            # A multi-byte character may be cut at the end of the sample.
            try:
                head[:-4].decode("utf-8")
            except UnicodeDecodeError:
                return False
        return True
    return False


def validate(upload):
    """Return (extension, mime type) or raise UploadRejected."""
    name = getattr(upload, "name", "") or ""
    ext = _extension(name)
    if ext not in ALLOWED:
        raise UploadRejected(
            "That file type isn't allowed. Use PDF, Word, Excel, PowerPoint, CSV, text, PNG or JPEG."
        )
    if upload.size == 0:
        raise UploadRejected("The file is empty.")
    if upload.size > max_upload_bytes():
        raise UploadRejected(f"Files can be at most {max_upload_bytes() // (1024 * 1024)} MB.")
    upload.seek(0)
    head = upload.read(4096)
    if not _looks_like(ext, head, upload):
        raise UploadRejected("The file's contents don't match its type. It may be damaged or renamed.")
    upload.seek(0)
    return ext, ALLOWED[ext]


def save(upload, department):
    ext, mime = validate(upload)
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    key = f"{department.pk}/{uuid.uuid4().hex}.{ext}"
    stored_key = storage().save(key, File(upload))
    return StoredFile(
        storage_key=stored_key, mime_type=mime, size=upload.size, checksum=digest.hexdigest(), extension=ext
    )


def open_file(document):
    return storage().open(document.storage_key, "rb")


def delete(storage_key):
    if storage_key and storage().exists(storage_key):
        storage().delete(storage_key)
