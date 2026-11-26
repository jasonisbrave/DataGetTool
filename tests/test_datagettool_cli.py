from __future__ import annotations

from pathlib import Path

import gzip
import pytest

from datagettool_core import ToolError, extract_ba, inspect_bak, process_xml


def test_inspect_unknown(tmp_path: Path):
    with pytest.raises(ToolError):
        inspect_bak(str(tmp_path / "missing.bak"))


def test_ba_gzip(tmp_path: Path):
    source = tmp_path / "backup.ba_"
    with gzip.open(source, "wb") as handle:
        handle.write(b"SQL Server backup bytes")
    output = tmp_path / "out"
    output.mkdir()
    result = extract_ba(str(source), str(output))
    assert result.get("ok") is True
    assert result.get("bak_files")


def test_xml_utf8(tmp_path: Path):
    source = tmp_path / "data.xml"
    source.write_text('<?xml version="1.0" encoding="utf-8"?><root />', encoding="utf-8")
    result = process_xml(str(source))
    assert result.get("ok") is True
