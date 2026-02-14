from __future__ import annotations

import os
import tempfile
import unittest
import zipfile

from tgparser.utils.tdata import TdataArchiveError, extract_tdata_from_archive


class TestExtractTdataFromArchive(unittest.TestCase):
    def test_extract_root_level_tdata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            archive = os.path.join(td, "tdata.zip")
            extract_root = os.path.join(td, "out")

            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("tdata/key_datas", b"abc")

            tdata_dir = extract_tdata_from_archive(archive_path=archive, extract_root=extract_root)
            self.assertTrue(tdata_dir.endswith(os.path.join("out", "tdata")))
            self.assertTrue(os.path.isfile(os.path.join(tdata_dir, "key_datas")))

    def test_extract_nested_tdata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            archive = os.path.join(td, "tdata.zip")
            extract_root = os.path.join(td, "out")

            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("Desktop/tdata/key_datas", b"abc")

            tdata_dir = extract_tdata_from_archive(archive_path=archive, extract_root=extract_root)
            self.assertTrue(tdata_dir.endswith(os.path.join("Desktop", "tdata")))

    def test_reject_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            archive = os.path.join(td, "evil.zip")
            extract_root = os.path.join(td, "out")

            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("../tdata/key_datas", b"abc")

            with self.assertRaises(TdataArchiveError):
                extract_tdata_from_archive(archive_path=archive, extract_root=extract_root)


if __name__ == "__main__":
    unittest.main()
