"""Offline regression checks for installing the VC++ runtime DLLs into a prefix.

Run with PYTHONPATH=src/mo2-lint:src python -m unittest discover -s tests.
"""

import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from step import configure_prefix
from util import variables as var


def fake_dll(
    path: Path, version: tuple[int, int, int, int] | None = None, builtin=False
):
    """Writes a file with the parts of a DLL the installer inspects."""
    header = bytearray(0x80)
    header[:2] = b"MZ"
    if builtin:
        header[0x40:0x50] = b"Wine builtin DLL"
    data = bytes(header)
    if version:
        major, minor, build, revision = version
        data += b"\xbd\x04\xef\xfe" + struct.pack(
            "<III", 0x10000, major << 16 | minor, build << 16 | revision
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class VCRedistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / "compatdata" / "489830"
        self.windows = self.prefix / "pfx" / "drive_c" / "windows"
        (self.windows / "system32").mkdir(parents=True)
        (self.windows / "syswow64").mkdir(parents=True)
        for context in (
            patch.object(var, "prefix", self.prefix, create=True),
            patch.object(configure_prefix.host, "is_arm64", return_value=False),
        ):
            context.start()
            self.addCleanup(context.stop)

    def package(self, arch: str, versions: dict[str, tuple[int, int, int, int]]):
        """Patches extraction to return DLLs as they are named inside the cabinets."""
        directory = self.root / f"package-{arch}"
        suffix = configure_prefix.vcredist_suffixes[arch]
        dlls = [
            fake_dll(directory / f"{name}.dll{suffix}", version)
            for name, version in versions.items()
        ]
        context = patch.object(
            configure_prefix, "_extract_vcredist_dlls", return_value=dlls
        )
        context.start()
        self.addCleanup(context.stop)

    def test_windows_dir_supports_proton_and_wine_prefixes(self):
        self.assertEqual(configure_prefix._windows_dir(), self.windows)

        wine_prefix = self.root / "heroic"
        (wine_prefix / "drive_c" / "windows").mkdir(parents=True)
        with patch.object(var, "prefix", wine_prefix, create=True):
            self.assertEqual(
                configure_prefix._windows_dir(), wine_prefix / "drive_c" / "windows"
            )

    def test_should_replace(self):
        new = fake_dll(self.root / "new.dll", (14, 44, 35211, 0))
        self.assertTrue(
            configure_prefix._should_replace(self.root / "missing.dll", new)
        )
        builtin = fake_dll(self.root / "builtin.dll", (99, 0, 0, 0), builtin=True)
        self.assertTrue(configure_prefix._should_replace(builtin, new))
        old = fake_dll(self.root / "old.dll", (14, 0, 24215, 1))
        self.assertTrue(configure_prefix._should_replace(old, new))
        same = fake_dll(self.root / "same.dll", (14, 44, 35211, 0))
        self.assertFalse(configure_prefix._should_replace(same, new))
        newer = fake_dll(self.root / "newer.dll", (14, 50, 35719, 0))
        self.assertFalse(configure_prefix._should_replace(newer, new))

    def test_replaces_steam_2015_runtime(self):
        system32 = self.windows / "system32"
        fake_dll(system32 / "msvcp140.dll", (14, 0, 24215, 1))
        fake_dll(system32 / "vcruntime140_1.dll", (14, 0, 0, 0), builtin=True)
        self.package(
            "x64",
            {"msvcp140": (14, 44, 35211, 0), "vcruntime140_1": (14, 44, 35211, 0)},
        )

        configure_prefix._install_vcredist_dlls(Path("VC_redist.x64.exe"), "x64")

        for name in ("msvcp140.dll", "vcruntime140_1.dll"):
            self.assertEqual(
                configure_prefix._file_version(system32 / name), (14, 44, 35211, 0)
            )
        self.assertEqual(list((self.windows / "syswow64").iterdir()), [])

    def test_installs_x86_runtime_to_syswow64(self):
        self.package("x86", {"msvcp140": (14, 44, 35211, 0)})

        configure_prefix._install_vcredist_dlls(Path("VC_redist.x86.exe"), "x86")

        self.assertEqual(
            configure_prefix._file_version(self.windows / "syswow64" / "msvcp140.dll"),
            (14, 44, 35211, 0),
        )
        self.assertEqual(list((self.windows / "system32").iterdir()), [])

    def test_keeps_newer_runtime(self):
        target = fake_dll(
            self.windows / "system32" / "msvcp140.dll", (14, 50, 35719, 0)
        )
        self.package("x64", {"msvcp140": (14, 44, 35211, 0)})

        configure_prefix._install_vcredist_dlls(Path("VC_redist.x64.exe"), "x64")

        self.assertEqual(configure_prefix._file_version(target), (14, 50, 35719, 0))

    def test_does_not_write_through_symlinks(self):
        proton_dll = fake_dll(self.root / "proton" / "msvcp140.dll", builtin=True)
        target = self.windows / "system32" / "msvcp140.dll"
        target.symlink_to(proton_dll)
        self.package("x64", {"msvcp140": (14, 44, 35211, 0)})

        configure_prefix._install_vcredist_dlls(Path("VC_redist.x64.exe"), "x64")

        self.assertFalse(target.is_symlink())
        self.assertEqual(configure_prefix._file_version(target), (14, 44, 35211, 0))
        self.assertTrue(configure_prefix._is_wine_builtin(proton_dll))

    def test_skips_64_bit_runtime_in_32_bit_prefix(self):
        (self.windows / "syswow64").rmdir()
        self.package("x64", {"msvcp140": (14, 44, 35211, 0)})

        configure_prefix._install_vcredist_dlls(Path("VC_redist.x64.exe"), "x64")

        self.assertEqual(list((self.windows / "system32").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
