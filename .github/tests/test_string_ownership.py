import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get("PDFA_OWNERSHIP_SOURCE", ROOT / "PDFA/PDFA-4dplugin.cpp"))
SDK = ROOT / "4D-Plugin-SDK/4D Plugin API"


class StringOwnershipTests(unittest.TestCase):
    def test_status_string_lifetime(self):
        source = SOURCE.read_text()
        # Compile the actual helpers without the unrelated PoDoFo conversion/build.
        start = "static std::string uniToUtf8("
        end = "/* read file into buffer */"
        self.assertEqual(source.count(start), 1)
        self.assertEqual(source.count(end), 1)
        self.assertEqual(source.count("static void returnStatus("), 1)
        self.assertLess(source.index(start), source.index("static void returnStatus("))
        self.assertLess(source.index("static void returnStatus("), source.index(end))
        helpers = source[source.index(start):source.index(end)]
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            (build / "status_helpers.inc").write_text(helpers)
            flags = ["-g", "-O1", "-fsanitize=address,undefined",
                     "-fno-omit-frame-pointer", "-I", str(SDK)]
            subprocess.run(
                ["clang", *flags, "-Wno-int-conversion", "-Wno-multichar",
                 "-c", str(SDK / "4DPluginAPI.c"), "-o", str(build / "sdk.o")],
                check=True,
            )
            executable = build / "string_ownership"
            subprocess.run(
                ["clang++", *flags, "-std=c++17", "-Wno-deprecated-declarations",
                 "-I", str(build),
                 str(ROOT / ".github/tests/string_ownership.cpp"), str(build / "sdk.o"),
                 "-Wl,-dead_strip", "-framework", "CoreGraphics",
                 "-framework", "CoreFoundation", "-o", str(executable)],
                check=True,
            )
            subprocess.run([str(executable)], check=True,
                           env={**os.environ, "UBSAN_OPTIONS": "halt_on_error=1",
                                "ASAN_OPTIONS": "halt_on_error=1"})


if __name__ == "__main__":
    unittest.main()
