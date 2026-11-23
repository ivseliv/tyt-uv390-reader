import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "macOS SwiftUI tests")
class ServiceFileTests(unittest.TestCase):
    def run_check(self, check, *sources):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as scratch:
            dest = Path(scratch)
            program = dest / "service-file-check"
            subprocess.run(["swiftc", "-swift-version", "6", "-parse-as-library", "-warnings-as-errors",
                            "-module-cache-path", str(root / "dist" / ".swift-module-cache"),
                            root / "macos/Localization.swift", root / "macos/LocalCopy.swift", *(root / "macos" / source for source in sources),
                            root / "tests" / check, "-o", program], check=True)
            subprocess.run([program, dest], check=True)

    def test_offline_calibration_and_json_roundtrip(self):
        self.run_check("calibration_roundtrip.swift", "CalibrationEditor.swift")

    def test_offline_dec_and_rdt_roundtrip(self):
        self.run_check("PrivacyFileChecks.swift", "PrivacyEditor.swift", "RDTPrivacyEditor.swift")

    def test_universal_usb_metadata_and_local_key(self):
        self.run_check("UniversalUSBChecks.swift", "PrivacyEditor.swift", "UniversalUSBViewer.swift")

    def test_radio_write_review_and_confirmation(self):
        self.run_check("RadioWriteReviewChecks.swift", "RadioWriteReview.swift")
