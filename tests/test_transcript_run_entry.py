"""Entry-point tests for the transcript route.

These exist because of a specific failure: `--prd` was shipped with a guessed
`extract_document_text` import that no test and no import check ever executed —
lazy imports inside a function body are invisible to both — so the flag crashed
on first contact with a real .docx. Anything reached only through a branch of
`_read_request` gets a test here.
"""
from __future__ import annotations

import pytest

from shamsu.transcript.run import _read_request, main


def test_plain_request_passes_through(tmp_path):
    assert _read_request(tmp_path, "build a thing", None) == "build a thing"


def test_text_file_prd_is_read(tmp_path):
    (tmp_path / "spec.md").write_text("# Spec\n\nBuild the marketplace.", encoding="utf-8")
    text = _read_request(tmp_path, "", "spec.md")
    assert "Build the marketplace." in text


def test_request_is_prepended_to_the_prd(tmp_path):
    (tmp_path / "spec.md").write_text("PRD BODY", encoding="utf-8")
    text = _read_request(tmp_path, "Use Django.", "spec.md")
    assert text.startswith("Use Django.")
    assert "PRD BODY" in text


def test_prd_path_may_be_absolute(tmp_path):
    source = tmp_path / "spec.txt"
    source.write_text("ABSOLUTE BODY", encoding="utf-8")
    assert "ABSOLUTE BODY" in _read_request(tmp_path, "", str(source))


def test_missing_prd_fails_loudly(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        _read_request(tmp_path, "", "nope.docx")
    assert "not found" in str(excinfo.value)


def test_binary_document_extractor_is_importable():
    """The exact import that shipped broken.

    Asserted by import rather than by parsing a real .docx so the test needs no
    fixture binary, but it is the identical symbol `_read_request` resolves.
    """
    from shamsu.tools.workspace import extract_document_text

    assert callable(extract_document_text)


@pytest.mark.parametrize("suffix", [".docx", ".pdf"])
def test_binary_suffixes_route_to_the_extractor(tmp_path, monkeypatch, suffix):
    source = tmp_path / f"spec{suffix}"
    source.write_bytes(b"not really a document")
    called: list = []

    def fake_extract(path):
        called.append(path)
        return "EXTRACTED TEXT"

    monkeypatch.setattr("shamsu.tools.workspace.extract_document_text", fake_extract)
    text = _read_request(tmp_path, "Use Django.", source.name)
    assert called == [source]
    assert "EXTRACTED TEXT" in text
    assert text.startswith("Use Django.")


def test_empty_request_and_no_prd_is_rejected(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main([str(tmp_path)])
