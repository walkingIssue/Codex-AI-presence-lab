from __future__ import annotations

from presence_runtime.cli import emit


def test_emit_escapes_catalog_unicode_for_legacy_windows_consoles(capsys) -> None:
    emit({"entrypoint": "彼岸/model.json"}, compact=True)

    output = capsys.readouterr().out
    assert "\\u5f7c\\u5cb8" in output
    assert "彼岸" not in output
