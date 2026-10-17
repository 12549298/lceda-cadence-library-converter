import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import json

from convert import ConversionError, convert, package_job, package_name, parser, result_directory


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dest = self.root / "我的器件库"
        (self.dest / "symbol").mkdir(parents=True)
        (self.dest / "footprint").mkdir()
        self.job = self.root / "job"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, data):
        path = self.job / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_symbol_and_footprint_are_siblings_with_only_deliverables(self):
        self.write("schematic/PROJECT_WORKLIB.OLB", b"library")
        self.write("schematic/INPUT.DSN", b"design")
        self.write("schematic/PROJECT_WORKLIB.OLBlck", b"lock")
        package_job(self.job, "schdoc", Path("电源.SchDoc"), self.dest, "001")
        for name in ("part.dra", "part.psm", "pad.pad", "shape.ssm"):
            self.write("library/" + name, name.encode())
        self.write("library/devices/device.txt", b"device")
        self.write("library/dump_libraries.log", b"diagnostic")
        package_job(self.job, "pcbdoc", Path("pcb.PcbDoc"), self.dest, "002")
        self.assertEqual({p.name for p in self.dest.iterdir()}, {"symbol", "footprint"})
        self.assertEqual({p.name for p in (self.dest / "symbol").iterdir()}, {"电源.olb"})
        self.assertEqual({p.name for p in (self.dest / "footprint").iterdir()},
                         {"part.dra", "part.psm", "pad.pad", "shape.ssm", "device.txt"})

    def test_conflicting_pad_rejects_whole_job_without_overwriting(self):
        (self.dest / "footprint/COMMON.pad").write_bytes(b"existing")
        self.write("library/new.psm", b"new symbol")
        self.write("library/common.pad", b"different pad")
        published = {"footprint/common.pad": self.dest / "footprint/COMMON.pad"}
        with self.assertRaises(ConversionError):
            package_job(self.job, "pcbdoc", Path("pcb.PcbDoc"), self.dest, "001", published=published)
        self.assertFalse((self.dest / "footprint/new.psm").exists())
        self.assertEqual((self.dest / "footprint/COMMON.pad").read_bytes(), b"existing")

    def test_identical_dependencies_are_shared_without_renaming(self):
        self.write("library/common.pad", b"shared pad")
        self.write("library/one.psm", b"symbol")
        package_job(self.job, "pcbdoc", Path("a.PcbDoc"), self.dest, "001")
        package_job(self.job, "pcbdoc", Path("b.PcbDoc"), self.dest, "002")
        self.assertEqual(len(list((self.dest / "footprint").iterdir())), 2)

    def test_different_symbol_libraries_with_same_source_name_survive(self):
        published = {}
        self.write("schematic/PROJECT_WORKLIB.OLB", b"first")
        package_job(self.job, "schdoc", Path("a/main.SchDoc"), self.dest, "001", published=published)
        self.job = self.root / "second_job"
        self.write("schematic/PROJECT_WORKLIB.OLB", b"second")
        package_job(self.job, "schdoc", Path("b/main.SchDoc"), self.dest, "002", published=published)
        self.assertEqual((self.dest / "symbol/main.olb").read_bytes(), b"first")
        self.assertEqual((self.dest / "symbol/main_002.olb").read_bytes(), b"second")

    def test_overwrite_previous_library_preserves_unrelated_files(self):
        old = self.dest / "footprint/COMMON.pad"
        old.write_bytes(b"old pad")
        note = self.dest / "footprint/notes.txt"
        note.write_bytes(b"keep")
        self.write("library/common.pad", b"updated pad")
        self.write("library/new.psm", b"new symbol")
        package_job(self.job, "pcbdoc", Path("pcb.PcbDoc"), self.dest, "001")
        self.assertEqual(old.read_bytes(), b"updated pad")
        self.assertEqual(note.read_bytes(), b"keep")
        self.assertTrue((self.dest / "footprint/new.psm").exists())
        self.assertEqual(len(list((self.dest / "footprint").glob("*.tmp"))), 0)

    def test_copy_failure_does_not_truncate_previous_library(self):
        old = self.dest / "symbol/main.olb"
        old.write_bytes(b"old library")
        self.write("schematic/PROJECT_WORKLIB.OLB", b"new library")
        with patch("convert.shutil.copyfileobj", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                package_job(self.job, "schdoc", Path("main.SchDoc"), self.dest, "001")
        self.assertEqual(old.read_bytes(), b"old library")
        self.assertEqual({p.name for p in old.parent.iterdir()}, {"main.olb"})

    def test_reexport_same_folder_updates_symbol_without_suffix(self):
        source = self.root / "source.SchDoc"
        source.write_text("|HEADER=Protel for Windows - Schematic Capture Ascii File Version 5.0|WEIGHT=1\n")
        args = parser().parse_args([str(source), "--output", str(self.root / "exports"),
                                   "--name", "same_folder", "--work-root", str(self.root / "work")])
        data = [b"first"]
        def export(job, *unused):
            (job / "schematic").mkdir()
            (job / "schematic/PROJECT_WORKLIB.OLB").write_bytes(data[0])
            return {"status": "success"}
        with patch("convert.SETTINGS_FILE", self.root / "settings.json"), \
             patch("convert.find_cadence", return_value=self.root), \
             patch("convert.execute_job", side_effect=export):
            first = convert(args, lambda message: None)
            data[0] = b"second"
            second = convert(args, lambda message: None)
        self.assertEqual(result_directory(first), result_directory(second))
        self.assertEqual(json.loads(second.read_text(encoding="utf-8"))["status"], "success")
        symbols = result_directory(second) / "symbol"
        self.assertEqual({p.name for p in symbols.iterdir()}, {"source.olb"})
        self.assertEqual((symbols / "source.olb").read_bytes(), b"second")

    def test_name_cannot_escape_selected_output_directory(self):
        for name in ("", "..", "../other", "a/b", "a\\b", "C:other", "CON", "NUL.txt", "name."):
            with self.subTest(name=name), self.assertRaises(ConversionError):
                package_name(name)
        self.assertEqual(package_name("中文 器件库"), "中文 器件库")


if __name__ == "__main__":
    unittest.main()
