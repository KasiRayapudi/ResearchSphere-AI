"""
Pluggable antivirus abstraction for ResearchSphere AI.

No real scanner is wired up yet. This module defines the contract so a real
engine can be dropped in without touching the upload pipeline:

    class ClamAVScanner(VirusScanner):
        name = "clamav"
        def scan_file(self, path: str) -> ScanResult:
            ...

then set ``VIRUS_SCANNER=clamav`` and register it in ``_SCANNERS``.

The default ``NoOpScanner`` returns ``clean`` so behaviour is unchanged until a
real scanner is configured. It reports ``scanned=False`` so the upload pipeline
can record in the audit trail that no actual scanning took place - a "clean"
result from the no-op scanner must never be mistaken for a real verdict.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("antivirus")


@dataclass
class ScanResult:
    """Outcome of a virus scan."""

    is_clean: bool
    scanner: str
    #: False when no real scanning occurred (e.g. the no-op scanner).
    scanned: bool = False
    threat: str | None = None
    details: str | None = None

    @property
    def is_infected(self) -> bool:
        return not self.is_clean

    def to_metadata(self) -> dict:
        return {
            "scanner": self.scanner,
            "scanned": self.scanned,
            "is_clean": self.is_clean,
            "threat": self.threat,
        }


class VirusScanner(ABC):
    """Interface every scanner implementation must satisfy."""

    name: str = "base"

    @abstractmethod
    def scan_file(self, path: str) -> ScanResult:
        """Scan a file on disk."""

    def scan(self, data: bytes) -> ScanResult:
        """Scan an in-memory buffer.

        Defaults to writing to a temporary file and delegating to
        ``scan_file`` so implementations only have to provide one of the two.
        """
        import os
        import tempfile

        handle, tmp_path = tempfile.mkstemp(prefix="rs_scan_")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(data)
            return self.scan_file(tmp_path)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:  # pragma: no cover
                pass


class NoOpScanner(VirusScanner):
    """Default scanner: performs no scanning and reports the file as clean.

    ``scanned=False`` makes the absence of real scanning explicit downstream.
    """

    name = "noop"

    def scan_file(self, path: str) -> ScanResult:
        return ScanResult(
            is_clean=True,
            scanner=self.name,
            scanned=False,
            details="No antivirus engine configured; file was not scanned.",
        )


#: Registry of available scanners. Add real implementations here.
_SCANNERS = {
    "noop": NoOpScanner,
    "none": NoOpScanner,
    "disabled": NoOpScanner,
}

_instance: VirusScanner | None = None


def get_scanner() -> VirusScanner:
    """Return the configured scanner (singleton).

    An unknown scanner name falls back to the no-op scanner with a warning
    rather than failing the upload path outright.
    """
    global _instance
    if _instance is None:
        configured = (getattr(settings, "VIRUS_SCANNER", "noop") or "noop").lower()
        scanner_cls = _SCANNERS.get(configured)
        if scanner_cls is None:
            logger.warning(f"Unknown VIRUS_SCANNER '{configured}'; falling back to no-op scanner.")
            scanner_cls = NoOpScanner
        _instance = scanner_cls()
        logger.info(f"Antivirus scanner initialised: {_instance.name}")
    return _instance


def reset_scanner() -> None:
    """Clear the cached scanner (used by tests)."""
    global _instance
    _instance = None
