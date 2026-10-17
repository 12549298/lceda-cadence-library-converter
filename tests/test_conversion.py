import tempfile
import unittest
import zipfile
from pathlib import Path

import ad_bridge
from convert import ConversionError, detect, discover, normalize_pcb, parser, safe_unzip


PCB = "|RECORD=Board|KIND=Protel_Advanced_PCB|VERSION=5.01\n"
COMP = "|RECORD=Component|ID=0|PATTERN=SOT-23|SOURCEDESIGNATOR=U1|X=10mil|Y=20mil\n"
PAD = "|RECORD=Pad|COMPONENT=0|NAME=1|X=10mil|Y=20mil|XSIZE=12mil|YSIZE=30mil|HOLESIZE=0mil\n"
TEXT = "|RECORD=Text|TEXT=U1|X=5mil|Y=8mil|HEIGHT=45mil|ROTATION=90\n"
SCH = "|HEADER=Protel for Windows - Schematic Capture Ascii File Version 5.0|WEIGHT=1\n"


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_content_detection_not_extension(self):
        self.assertTrue(detect(self.write("pcb.PcbDoc", PCB))["ok"])
        self.assertTrue(detect(self.write("sch.SchDoc", SCH))["ok"])
        self.assertFalse(detect(self.write("fake.PcbDoc", "not a PCB"))["ok"])
        p = self.root / "binary.PcbDoc"
        p.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"fake data")
        self.assertIn("二进制", detect(p)["reason"])

    def test_strict_by_default_and_no_partial_file(self):
        source = self.write("raw.PcbDoc", PCB + COMP + PAD + TEXT)
        output = self.root / "out.PcbDoc"
        self.assertTrue(parser().parse_args([]).strict_text)
        with self.assertRaises(ConversionError):
            normalize_pcb(source, output)
        self.assertFalse(output.exists())
        self.assertFalse(parser().parse_args(["--allow-text-estimation"]).strict_text)

    def test_opt_in_estimation_preserves_source_and_geometry(self):
        content = PCB + COMP + PAD + TEXT
        source = self.write("raw.PcbDoc", content)
        output = self.root / "out.PcbDoc"
        report = normalize_pcb(source, output, strict_text=False)
        self.assertEqual(source.read_text(), content)
        self.assertEqual(report["estimated_text_bounds"], 1)
        self.assertTrue(report["warnings"])
        self.assertFalse(output.read_bytes().startswith(b"\xef\xbb\xbf"))
        ad_bridge.validate_geometry(source, output)
        self.assertEqual(ad_bridge.missing_text_bounds(output), 0)

    def test_existing_indexes_not_overwritten_or_reused(self):
        source = self.write("raw.PcbDoc", PCB + PAD.rstrip() + "|INDEXFORSAVE=0\n" + PAD.replace("NAME=1", "NAME=2"))
        output = self.root / "out.PcbDoc"
        normalize_pcb(source, output)
        records = [r for r in ad_bridge.read_pcb(output) if r.get("RECORD") == "Pad"]
        self.assertEqual([r["INDEXFORSAVE"] for r in records], ["0", "1"])

    def test_existing_bounds_untouched(self):
        source = self.write("raw.PcbDoc", PCB + TEXT.rstrip() + "|X1=1mil|Y1=2mil|X2=3mil|Y2=4mil\n")
        output = self.root / "out.PcbDoc"
        report = normalize_pcb(source, output)
        self.assertEqual(report["estimated_text_bounds"], 0)
        self.assertIn("|X1=1mil|Y1=2mil|X2=3mil|Y2=4mil", output.read_text())

    def test_geometry_validation_detects_pad_change(self):
        source = self.write("raw.PcbDoc", PCB + COMP + PAD)
        changed = self.write("changed.PcbDoc", PCB + COMP + PAD.replace("XSIZE=12mil", "XSIZE=13mil"))
        with self.assertRaises(RuntimeError):
            ad_bridge.validate_geometry(source, changed)

    def test_geometry_validation_allows_units_and_number_format(self):
        source = self.write("raw.PcbDoc", PCB + PAD)
        saved = self.write("saved.PcbDoc", PCB + PAD.replace("X=10mil", "X=0.254mm").replace("YSIZE=30mil", "YSIZE=3.0E1mil"))
        ad_bridge.validate_geometry(source, saved)

    def test_reject_unsafe_archive_paths(self):
        for name in ("../escape.PcbDoc", "C:/escape.PcbDoc", "/escape.PcbDoc", "a:stream", "CON.txt", "a/../b"):
            with self.subTest(name=name):
                z = self.root / "bad.zip"
                with zipfile.ZipFile(z, "w") as archive:
                    archive.writestr(name, PCB)
                with self.assertRaises(ConversionError):
                    safe_unzip(z, self.root / "out")

    def test_reject_case_collision_in_zip(self):
        z = self.root / "case.zip"
        with zipfile.ZipFile(z, "w") as archive:
            archive.writestr("A.PcbDoc", PCB)
            archive.writestr("a.pcbdoc", PCB)
        with self.assertRaises(ConversionError):
            safe_unzip(z, self.root / "out")

    def test_discovery_skips_history_and_previous_output(self):
        good = self.write("input/中文 空格/pcb.PcbDoc", PCB)
        self.write("input/History/old.PcbDoc", PCB)
        self.write("input/output/run/input.PcbDoc", PCB)
        _, files = discover(self.root / "input", self.root / "work", excluded=(self.root / "input/output",))
        self.assertEqual(files, [good])


if __name__ == "__main__":
    unittest.main()
