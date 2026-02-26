from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Dict, List

from pigeon.common import tail_jsonl


def _collect(path: Path, offset: int) -> tuple[int, List[Dict[str, object]]]:
    new_offset, it = tail_jsonl(path, offset)
    return new_offset, list(it)


class TailJsonlTests(unittest.TestCase):
    def test_partial_line_is_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            p.write_text('{"k":1', encoding="utf-8")

            off, records = _collect(p, 0)
            self.assertEqual(off, 0)
            self.assertEqual(records, [])

            with p.open("a", encoding="utf-8") as fh:
                fh.write("}\n")

            off, records = _collect(p, off)
            self.assertEqual(records, [{"k": 1}])
            self.assertEqual(off, p.stat().st_size)

    def test_mixed_complete_and_partial_lines_preserve_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            p.write_text('{"a":1}\n{"b":2', encoding="utf-8")

            off, records = _collect(p, 0)
            self.assertEqual(records, [{"a": 1}])

            with p.open("a", encoding="utf-8") as fh:
                fh.write("}\n")

            off, records = _collect(p, off)
            self.assertEqual(records, [{"b": 2}])
            self.assertEqual(off, p.stat().st_size)


if __name__ == "__main__":
    unittest.main()
