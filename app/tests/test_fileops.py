import json
from pathlib import Path

from mena.fileops import write_json_atomically


def test_write_json_atomically_writes_sorted_indented_json_with_newline(tmp_path: Path) -> None:
    path = tmp_path / "record.json"

    write_json_atomically(path, {"b": 2, "a": 1})

    assert path.read_text(encoding="utf-8") == '{\n  "a": 1,\n  "b": 2\n}\n'


def test_write_json_atomically_replaces_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    path.write_text(json.dumps({"old": True}), encoding="utf-8")

    write_json_atomically(path, {"new": True})

    assert json.loads(path.read_text(encoding="utf-8")) == {"new": True}
    assert not (tmp_path / ".record.json.tmp").exists()
