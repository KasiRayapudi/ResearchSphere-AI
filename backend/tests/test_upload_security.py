"""Unit tests for the upload security layer."""

import io
import os
import zipfile

import pytest

from app.core.config import settings
from app.core.upload_security import (
    UploadValidationError,
    build_storage_filename,
    detect_file_kind,
    extract_extension,
    resolve_within,
    sanitize_filename,
    validate_content,
    validate_extension,
)

pytestmark = pytest.mark.unit

PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n" + b"x" * 400
)
TEXT_BYTES = b"Plain text content for validation. " * 30


def _zip(entries: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


DOCX_BYTES = _zip({"[Content_Types].xml": "<Types/>", "word/document.xml": "<w:document/>"})
PPTX_BYTES = _zip({"[Content_Types].xml": "<Types/>", "ppt/presentation.xml": "<p:pres/>"})
JAR_BYTES = _zip({"META-INF/MANIFEST.MF": "Manifest-Version: 1.0", "Main.class": "\x00"})


class TestFilenameSanitization:
    @pytest.mark.parametrize(
        "raw",
        [
            "../../../etc/passwd",
            "..\\..\\windows\\system32\\cmd.exe",
            "/absolute/path/doc.pdf",
            "C:\\Users\\victim\\doc.pdf",
        ],
    )
    def test_directory_components_are_stripped(self, raw):
        result = sanitize_filename(raw)
        assert "/" not in result
        assert "\\" not in result
        assert ".." not in result

    def test_control_characters_removed(self):
        assert "\x00" not in sanitize_filename("do\x00c.pdf")
        assert "\n" not in sanitize_filename("do\nc.pdf")

    def test_empty_name_falls_back(self):
        assert sanitize_filename("") == "unnamed"
        assert sanitize_filename(None) == "unnamed"

    def test_overlong_name_is_truncated(self):
        assert len(sanitize_filename("a" * 500 + ".pdf")) <= 255

    def test_extension_extracted_from_sanitized_name(self):
        assert extract_extension("../../report.PDF") == "pdf"
        assert extract_extension("noextension") == ""


class TestStorageNaming:
    def test_storage_name_is_opaque(self):
        name = build_storage_filename("pdf")
        assert name.endswith(".pdf")
        assert len(name) == 36  # 32 hex + dot + extension

    def test_storage_names_are_unique(self):
        assert build_storage_filename("pdf") != build_storage_filename("pdf")

    def test_resolve_within_blocks_escape(self):
        with pytest.raises(UploadValidationError):
            resolve_within(settings.UPLOAD_DIR, "../escape.txt")

    def test_resolve_within_allows_plain_name(self):
        resolved = resolve_within(settings.UPLOAD_DIR, "file.txt")
        assert resolved.endswith("file.txt")


class TestContentSniffing:
    @staticmethod
    def _write(tmp_path, name, data):
        path = tmp_path / name
        path.write_bytes(data)
        return str(path)

    def test_detects_pdf(self, tmp_path):
        assert detect_file_kind(self._write(tmp_path, "a.pdf", PDF_BYTES)) == "pdf"

    def test_detects_docx_by_zip_layout(self, tmp_path):
        assert detect_file_kind(self._write(tmp_path, "a.docx", DOCX_BYTES)) == "docx"

    def test_distinguishes_pptx_from_docx(self, tmp_path):
        assert detect_file_kind(self._write(tmp_path, "a.pptx", PPTX_BYTES)) == "pptx"

    def test_detects_plain_text(self, tmp_path):
        assert detect_file_kind(self._write(tmp_path, "a.txt", TEXT_BYTES)) == "text"

    @pytest.mark.parametrize(
        "data,expected",
        [
            (b"MZ\x90\x00" + b"\x00" * 100, "windows_executable"),
            (b"\x7fELF" + b"\x00" * 100, "elf_executable"),
            (b"#!/bin/bash\nrm -rf /", "script_shebang"),
            (b"\xca\xfe\xba\xbe" + b"\x00" * 100, "java_class"),
        ],
    )
    def test_detects_executables(self, tmp_path, data, expected):
        assert detect_file_kind(self._write(tmp_path, "a.bin", data)) == expected

    def test_detects_jar_by_manifest(self, tmp_path):
        assert detect_file_kind(self._write(tmp_path, "a.jar", JAR_BYTES)) == "jar"

    def test_empty_file_detected(self, tmp_path):
        assert detect_file_kind(self._write(tmp_path, "a.txt", b"")) == "empty"


class TestExtensionValidation:
    def test_allowed_extension_passes(self):
        validate_extension("pdf", settings.allowed_upload_extensions)

    @pytest.mark.parametrize("ext", ["exe", "bat", "sh", "js", "py", "jar", "zip", "dll"])
    def test_blocked_extensions_rejected(self, ext):
        with pytest.raises(UploadValidationError) as exc:
            validate_extension(ext, settings.allowed_upload_extensions)
        assert exc.value.status_code == 415

    def test_missing_extension_rejected(self):
        with pytest.raises(UploadValidationError) as exc:
            validate_extension("", settings.allowed_upload_extensions)
        assert exc.value.reason == "missing_extension"


class TestContentMatchesExtension:
    @staticmethod
    def _write(tmp_path, name, data):
        path = tmp_path / name
        path.write_bytes(data)
        return str(path)

    def test_matching_pdf_accepted(self, tmp_path):
        kind, mime = validate_content(self._write(tmp_path, "a.pdf", PDF_BYTES), "pdf")
        assert kind == "pdf"
        assert mime == "application/pdf"

    def test_matching_text_accepted(self, tmp_path):
        kind, mime = validate_content(self._write(tmp_path, "a.txt", TEXT_BYTES), "txt")
        assert kind == "text"
        assert mime == "text/plain"

    def test_executable_disguised_as_pdf_rejected(self, tmp_path):
        path = self._write(tmp_path, "payload.pdf", b"MZ\x90\x00" + b"\x00" * 500)
        with pytest.raises(UploadValidationError) as exc:
            validate_content(path, "pdf")
        assert "executable" in exc.value.reason

    def test_text_disguised_as_pdf_rejected(self, tmp_path):
        path = self._write(tmp_path, "fake.pdf", TEXT_BYTES)
        with pytest.raises(UploadValidationError) as exc:
            validate_content(path, "pdf")
        assert exc.value.reason.startswith("content_mismatch")

    def test_jar_rejected_even_with_allowed_extension(self, tmp_path):
        path = self._write(tmp_path, "a.docx", JAR_BYTES)
        with pytest.raises(UploadValidationError):
            validate_content(path, "docx")


class TestAntivirusInterface:
    def test_default_scanner_reports_no_real_scan(self, tmp_path):
        from app.services.antivirus import NoOpScanner

        probe = tmp_path / "f.txt"
        probe.write_bytes(TEXT_BYTES)
        result = NoOpScanner().scan_file(str(probe))
        assert result.is_clean is True
        # A "clean" verdict from the no-op scanner must never be mistaken for
        # a real one.
        assert result.scanned is False
        assert result.to_metadata()["scanner"] == "noop"

    def test_scan_bytes_delegates_to_scan_file(self):
        from app.services.antivirus import NoOpScanner

        assert NoOpScanner().scan(b"data").is_clean is True

    def test_unknown_scanner_falls_back_to_noop(self):
        from app.services import antivirus

        antivirus.reset_scanner()
        original = settings.VIRUS_SCANNER
        settings.VIRUS_SCANNER = "does-not-exist"
        try:
            assert antivirus.get_scanner().name == "noop"
        finally:
            settings.VIRUS_SCANNER = original
            antivirus.reset_scanner()


class TestStreamingHashAndSizeLimit:
    @pytest.mark.asyncio
    async def test_hash_matches_content_and_size_enforced(self, tmp_path):
        import hashlib

        from app.core.upload_security import FileTooLargeError, stream_to_quarantine

        class _Upload:
            """Minimal UploadFile stand-in exposing async read()."""

            def __init__(self, data: bytes):
                self._buffer = io.BytesIO(data)

            async def read(self, size: int = -1):
                return self._buffer.read(size)

        payload = b"content to hash " * 100
        stored = await stream_to_quarantine(
            _Upload(payload), str(tmp_path), "txt", max_bytes=10 * 1024 * 1024
        )
        assert stored.sha256 == hashlib.sha256(payload).hexdigest()
        assert stored.size == len(payload)
        assert os.path.exists(stored.path)

        # Oversized upload aborts mid-write and leaves nothing behind.
        with pytest.raises(FileTooLargeError):
            await stream_to_quarantine(_Upload(b"A" * 5000), str(tmp_path), "txt", max_bytes=1024)
