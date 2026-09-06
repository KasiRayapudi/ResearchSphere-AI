"""
Upload security primitives for ResearchSphere AI.

Validation is layered, and each layer assumes the previous one can be fooled:

1. Extension allowlist       - cheap rejection of obvious junk.
2. Content sniffing          - magic bytes decide the real type; the client's
                               declared Content-Type is never trusted.
3. Executable rejection      - explicit deny of executable signatures even if
                               the extension looked benign.
4. Extension/content match   - a .pdf whose bytes are a ZIP is rejected.

Size is enforced while streaming, so an oversized upload never lands on disk in
full, and the SHA-256 is computed in the same pass to avoid re-reading the file.

Nothing here writes to permanent storage: uploads land in the quarantine
directory and are promoted only after validation and scanning succeed.
"""
import hashlib
import os
import re
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("upload_security")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class UploadValidationError(Exception):
    """Raised when an upload fails a security check.

    ``status_code`` and ``reason`` map onto the HTTP response and the audit
    record respectively.
    """

    def __init__(self, message: str, reason: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.status_code = status_code


class FileTooLargeError(UploadValidationError):
    def __init__(self, limit_mb: int):
        super().__init__(
            message=f"File exceeds the maximum upload size of {limit_mb} MB.",
            reason="file_too_large",
            status_code=413,  # Payload Too Large
        )


class UnsupportedFileTypeError(UploadValidationError):
    def __init__(self, message: str, reason: str = "unsupported_file_type"):
        super().__init__(message=message, reason=reason, status_code=415)


# ---------------------------------------------------------------------------
# Filename handling
# ---------------------------------------------------------------------------
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._\- ]")
_DOT_RUN = re.compile(r"\.{2,}")
_MAX_ORIGINAL_NAME = 255


def sanitize_filename(raw: Optional[str]) -> str:
    """Reduce a client-supplied filename to a safe, display-only string.

    This value is stored as metadata and never used to build a filesystem path
    - ``build_storage_filename`` generates the on-disk name. Sanitizing anyway
    means the original is safe to render, log, and return in JSON.
    """
    if not raw:
        return "unnamed"

    # Strip any directory component, including Windows-style separators that
    # os.path.basename ignores on POSIX.
    candidate = raw.replace("\\", "/").split("/")[-1]

    # Normalize unicode so lookalike/combining characters cannot smuggle
    # separators or control characters through.
    candidate = unicodedata.normalize("NFKD", candidate)
    candidate = "".join(ch for ch in candidate if ch.isprintable())

    candidate = _UNSAFE_CHARS.sub("_", candidate)
    candidate = _DOT_RUN.sub(".", candidate)          # defeat "..", "...."
    candidate = candidate.strip(". ").strip()          # no leading/trailing dots

    if not candidate:
        return "unnamed"
    if len(candidate) > _MAX_ORIGINAL_NAME:
        stem, dot, ext = candidate.rpartition(".")
        keep = _MAX_ORIGINAL_NAME - (len(ext) + 1) if dot else _MAX_ORIGINAL_NAME
        candidate = (stem[:keep] + dot + ext) if dot else candidate[:_MAX_ORIGINAL_NAME]
    return candidate


def extract_extension(filename: Optional[str]) -> str:
    """Lowercase extension without the dot, derived from a sanitized name."""
    safe = sanitize_filename(filename)
    if "." not in safe:
        return ""
    return safe.rsplit(".", 1)[-1].lower()


def build_storage_filename(extension: str) -> str:
    """Generate an opaque, collision-free on-disk filename.

    The client filename never contributes to the stored path, which removes
    path traversal and overwrite attacks by construction.
    """
    safe_ext = re.sub(r"[^a-z0-9]", "", (extension or "").lower())[:10]
    return f"{uuid.uuid4().hex}.{safe_ext}" if safe_ext else uuid.uuid4().hex


def resolve_within(directory: str, filename: str) -> str:
    """Join and verify the result stays inside ``directory``.

    A defence-in-depth check: ``build_storage_filename`` already produces safe
    names, but this guarantees no future caller can escape the directory.
    """
    base = Path(directory).resolve()
    target = (base / filename).resolve()
    if base != target.parent:
        raise UploadValidationError(
            message="Invalid file path.",
            reason="path_traversal_attempt",
            status_code=400,
        )
    return str(target)


# ---------------------------------------------------------------------------
# Content sniffing
# ---------------------------------------------------------------------------
#: Signatures that must never be accepted, whatever the extension claims.
_EXECUTABLE_SIGNATURES: Tuple[Tuple[bytes, str], ...] = (
    (b"MZ", "windows_executable"),            # .exe / .dll
    (b"\x7fELF", "elf_executable"),           # Linux binaries
    (b"\xca\xfe\xba\xbe", "java_class"),      # .class / Mach-O fat
    (b"\xfe\xed\xfa\xce", "mach_o"),
    (b"\xfe\xed\xfa\xcf", "mach_o"),
    (b"#!", "script_shebang"),                # .sh and friends
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole_compound_file"),  # legacy .doc/.xls
)

_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

#: Canonical MIME type per detected kind.
MIME_BY_KIND = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
}

#: Extensions whose content is plain text and therefore has no magic bytes.
TEXT_EXTENSIONS = {"txt", "md", "markdown", "csv"}


def _looks_like_text(sample: bytes) -> bool:
    """Heuristic: decodable as UTF-8/Latin-1, no NULs, mostly printable."""
    if b"\x00" in sample:
        return False
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = sample.decode("latin-1")
        except Exception:
            return False
    if not text:
        return True
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    return (printable / len(text)) > 0.90


def _inspect_zip(path: str) -> str:
    """Classify an OOXML container by its internal layout."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except Exception:
        return "zip"
    if any(n.startswith("word/") for n in names):
        return "docx"
    if any(n.startswith("ppt/") for n in names):
        return "pptx"
    if any(n.startswith("xl/") for n in names):
        return "xlsx"
    if "META-INF/MANIFEST.MF" in names:
        return "jar"
    return "zip"


def detect_file_kind(path: str) -> str:
    """Determine the real file type from content, ignoring the filename.

    Returns a short kind string ("pdf", "docx", "pptx", "xlsx", "text", "zip",
    "jar", "unknown") or an executable marker from ``_EXECUTABLE_SIGNATURES``.
    """
    with open(path, "rb") as handle:
        header = handle.read(8192)

    if not header:
        return "empty"

    for signature, label in _EXECUTABLE_SIGNATURES:
        if header.startswith(signature):
            return label

    if header.startswith(b"%PDF-"):
        return "pdf"

    if any(header.startswith(magic) for magic in _ZIP_MAGICS):
        return _inspect_zip(path)

    if _looks_like_text(header):
        return "text"

    return "unknown"


# ---------------------------------------------------------------------------
# Streaming write with size limit + hashing
# ---------------------------------------------------------------------------
CHUNK_SIZE = 1024 * 1024  # 1 MiB


@dataclass
class StoredUpload:
    """Result of streaming an upload into quarantine."""

    path: str
    size: int
    sha256: str


async def stream_to_quarantine(
    upload_file,
    quarantine_dir: str,
    extension: str,
    max_bytes: int,
) -> StoredUpload:
    """Stream an ``UploadFile`` to quarantine, enforcing the size cap.

    The cap is enforced *during* the write, so an oversized upload is aborted
    and removed rather than being written in full and measured afterwards.
    SHA-256 is computed in the same pass.
    """
    os.makedirs(quarantine_dir, exist_ok=True)
    storage_name = build_storage_filename(extension)
    target = resolve_within(quarantine_dir, storage_name)

    digest = hashlib.sha256()
    total = 0

    try:
        with open(target, "wb") as out:
            while True:
                chunk = await upload_file.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FileTooLargeError(limit_mb=max_bytes // (1024 * 1024))
                digest.update(chunk)
                out.write(chunk)
    except Exception:
        _safe_unlink(target)
        raise

    if total == 0:
        _safe_unlink(target)
        raise UploadValidationError(
            message="Uploaded file is empty.",
            reason="empty_file",
            status_code=400,
        )

    return StoredUpload(path=target, size=total, sha256=digest.hexdigest())


def _safe_unlink(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as exc:  # pragma: no cover
        logger.error(f"Failed to remove file {path}: {exc}")


def discard_quarantined(path: str) -> None:
    """Delete a rejected upload. Quarantined files are never served."""
    _safe_unlink(path)


def promote_from_quarantine(quarantine_path: str, upload_dir: str) -> str:
    """Move a validated file from quarantine into permanent storage."""
    os.makedirs(upload_dir, exist_ok=True)
    final_path = resolve_within(upload_dir, os.path.basename(quarantine_path))
    os.replace(quarantine_path, final_path)
    return final_path


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
@dataclass
class ValidatedUpload:
    original_filename: str
    extension: str
    detected_kind: str
    mime_type: str
    size: int
    sha256: str
    quarantine_path: str
    warnings: list = field(default_factory=list)


def validate_extension(extension: str, allowed: set) -> None:
    if not extension:
        raise UnsupportedFileTypeError(
            message="File has no extension. Supported types: "
            + ", ".join(sorted(allowed)),
            reason="missing_extension",
        )
    if extension in settings.blocked_upload_extensions:
        raise UnsupportedFileTypeError(
            message=f"Files of type '.{extension}' are not permitted.",
            reason="blocked_extension",
        )
    if extension not in allowed:
        raise UnsupportedFileTypeError(
            message=f"Unsupported file type '.{extension}'. Supported types: "
            + ", ".join(sorted(allowed)),
            reason="extension_not_allowed",
        )


def validate_content(path: str, extension: str) -> Tuple[str, str]:
    """Validate real content against the declared extension.

    Returns ``(detected_kind, mime_type)``.
    """
    kind = detect_file_kind(path)

    executable_labels = {label for _, label in _EXECUTABLE_SIGNATURES}
    if kind in executable_labels or kind == "jar":
        raise UnsupportedFileTypeError(
            message="Executable and script files are not permitted.",
            reason=f"executable_content:{kind}",
        )

    if kind == "empty":
        raise UploadValidationError(
            message="Uploaded file is empty.", reason="empty_file", status_code=400
        )

    normalized_ext = "md" if extension == "markdown" else extension

    if normalized_ext in TEXT_EXTENSIONS:
        if kind != "text":
            raise UnsupportedFileTypeError(
                message=(
                    f"File content does not match its '.{extension}' extension "
                    f"(detected: {kind})."
                ),
                reason=f"content_mismatch:{kind}",
            )
        return kind, MIME_BY_KIND.get(normalized_ext, "text/plain")

    if kind != normalized_ext:
        raise UnsupportedFileTypeError(
            message=(
                f"File content does not match its '.{extension}' extension "
                f"(detected: {kind})."
            ),
            reason=f"content_mismatch:{kind}",
        )

    return kind, MIME_BY_KIND.get(kind, "application/octet-stream")
