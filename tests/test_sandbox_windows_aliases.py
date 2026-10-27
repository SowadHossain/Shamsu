"""Windows extended paths must not create false escapes or allow real ones."""
import os
from pathlib import Path

import pytest

from shamsu.safety.sandbox import Sandbox, SecurityError, _comparison_path

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows path spellings")


def test_extended_drive_target_is_inside_the_ordinary_workspace(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    target = workspace / "file.txt"
    target.write_text("content")
    sandbox = Sandbox(workspace)
    assert sandbox.validate("\\\\?\\" + str(target)) == target.resolve()


def test_extended_workspace_accepts_the_ordinary_target(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    target = workspace / "file.txt"
    sandbox = Sandbox(Path("\\\\?\\" + str(workspace)))
    assert sandbox.validate(target) == target.resolve()


def test_extended_prefix_does_not_hide_a_sibling_escape(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "project-private" / "secret.txt"
    with pytest.raises(SecurityError):
        Sandbox(workspace).validate("\\\\?\\" + str(outside))


def test_unc_alias_is_normalized_without_reinterpreting_device_paths():
    assert _comparison_path(Path("\\\\?\\UNC\\server\\share\\folder")) == Path("\\\\server\\share\\folder")
    device = Path("\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy1\\secret")
    assert _comparison_path(device) == device
