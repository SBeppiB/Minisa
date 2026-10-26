"""Portable package construction and layout checks; never accesses USB."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import sys
import importlib.util
from unittest.mock import patch
import zipfile

import flash_firmware
import package_windows


class PackageTests(unittest.TestCase):
    def test_portable_layout_needs_no_server_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "firmware").mkdir()
            images, sketches = flash_firmware.project_layout(root)
            self.assertEqual(images, root / "firmware")
            self.assertEqual(sketches["scanner"], root / "firmware/scanner/scanner.ino")
            self.assertEqual(sketches["beacon"], root / "firmware/beacon/beacon.ino")

    def test_build_copies_exact_code_and_excludes_runtime_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "canonical"
            (root / "v3").mkdir(parents=True)
            (root / "beacon").mkdir()
            (root / "windows_validation").mkdir()
            for name in package_windows.V3_FILES:
                (root / "v3" / name).write_bytes(b"exact production snapshot\r\n")
            for name in package_windows.TEMPLATES:
                (root / "windows_validation" / name).write_text("template")
            (root / "beacon/flash_firmware.py").write_text("shared flasher")
            (root / "v3/PHYSICAL_VALIDATION.md").write_text("v3/validate_hardware.py")
            (root / "v3/minisa_senses.db").write_text("historical private data")
            (root / ".build/windows-wheels").mkdir(parents=True)
            (root / ".build/windows-wheels/esptool-5.3.1-py3-none-any.whl").write_bytes(b"fixture wheel")
            images, sketches = {}, {}
            for role in ("scanner", "beacon"):
                folder = root / ".build/full" / role
                folder.mkdir(parents=True)
                images[role] = folder / f"{role}.merged.bin"
                images[role].write_bytes(b"verified fixture image")
                (folder / "manifest.json").write_text("{}")
                sketches[role] = root / "beacon" / f"{role}.ino"
                sketches[role].write_text("firmware source")
            destination = Path(directory) / "portable"
            with patch.object(package_windows, "ROOT", root), \
                 patch.object(package_windows, "SKETCHES", sketches), \
                 patch.object(package_windows, "verified_image", side_effect=lambda role, folder: images[role]):
                archive = package_windows.build_package(destination)
            self.assertEqual((destination / "presence_service.py").read_bytes(), b"exact production snapshot\r\n")
            manifest = json.loads((destination / "package-manifest.json").read_text())
            for name, data in manifest["files"].items():
                self.assertEqual(hashlib.sha256((destination / name).read_bytes()).hexdigest(), data["sha256"])
            self.assertFalse(any(name.endswith(".db") for name in manifest["files"]))
            with zipfile.ZipFile(archive) as bundle:
                self.assertIsNone(bundle.testzip())
                self.assertEqual(set(bundle.namelist()), {"portable/" + name for name in manifest["files"]} | {"portable/package-manifest.json"})

    def test_refuses_to_overwrite_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileExistsError):
                package_windows.build_package(Path(directory))

    def test_invalid_firmware_stops_before_creating_kit(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "kit"
            with patch.object(package_windows, "verified_image", side_effect=ValueError("bad image")):
                with self.assertRaisesRegex(ValueError, "bad image"):
                    package_windows.build_package(destination)
            self.assertFalse(destination.exists())

    def test_flashing_executable_override_stays_explicit(self):
        self.assertEqual(flash_firmware.esptool_command(Path("chosen.exe")), ["chosen.exe"])

    def test_portable_flasher_uses_current_python_without_arduino(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "firmware").mkdir()
            with patch.object(flash_firmware, "__file__", str(root / "flash_firmware.py")):
                self.assertEqual(flash_firmware.esptool_command(), [sys.executable, "-m", "esptool"])

    def verifier(self):
        path = package_windows.ROOT / "windows_validation/verify_package.py"
        spec = importlib.util.spec_from_file_location("package_verifier", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_transfer_verification_detects_missing_and_changed_files(self):
        verifier = self.verifier()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "code.py").write_bytes(b"original")
            (root / "package-manifest.json").write_text(json.dumps(dict(files={
                "code.py": dict(sha256=hashlib.sha256(b"original").hexdigest())})))
            self.assertEqual(verifier.verify_files(root), [])
            (root / "code.py").write_bytes(b"changed")
            self.assertEqual(verifier.verify_files(root), ["Changed/corrupt: code.py"])
            (root / "code.py").unlink()
            self.assertEqual(verifier.verify_files(root), ["Missing: code.py"])

    def test_transfer_manifest_cannot_escape_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package-manifest.json").write_text(json.dumps(dict(files={
                "../outside": dict(sha256="x")})))
            self.assertEqual(self.verifier().verify_files(root), ["Unsafe manifest path: ../outside"])


if __name__ == "__main__":
    unittest.main()
