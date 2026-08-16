"""Safe staging for files received from Telegram."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from shamsu.integrations.telegram.models import TelegramFile
from shamsu.safety.sandbox import Sandbox

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class StagedTelegramFile:
    ok: bool
    path: Path | None = None
    filename: str = ""
    reason: str = ""


class TelegramFileStager:
    def __init__(self, workspace: Path, *, max_upload_bytes: int = MAX_UPLOAD_BYTES) -> None:
        self.workspace = Path(workspace).resolve()
        self.sandbox = Sandbox(self.workspace)
        self.max_upload_bytes = max_upload_bytes
        self.root = self.sandbox.validate(Path(".shamsu") / "telegram" / "attachments")
        self.root.mkdir(parents=True, exist_ok=True)

    def stage_metadata_only(self, document: TelegramFile, *, content: bytes = b"") -> StagedTelegramFile:
        if document.file_size > self.max_upload_bytes:
            return StagedTelegramFile(False, reason="File is too large for Telegram staging.")
        filename = sanitize_filename(document.file_name)
        target_dir = self.sandbox.validate(Path(".shamsu") / "telegram" / "attachments" / uuid.uuid4().hex[:12])
        target_dir.mkdir(parents=True, exist_ok=True)
        target = self.sandbox.validate(target_dir / filename)
        try:
            target.relative_to(self.root)
        except ValueError:
            return StagedTelegramFile(False, reason="Attachment path escaped the staging directory.")
        target.write_bytes(content)
        return StagedTelegramFile(True, path=target, filename=filename)


def sanitize_filename(name: str) -> str:
    basename = Path(name or "attachment").name
    clean = SAFE_NAME_RE.sub("_", basename).strip("._")
    return clean[:160] or "attachment"

