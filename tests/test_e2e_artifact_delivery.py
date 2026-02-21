"""
End-to-end test: simulates the EXACT iGB report scenario.

Reproduces the real-world pipeline:
  1. Project directory with pre-existing source files + __pycache__
  2. Script imports modules (creating/updating .pyc)
  3. Script writes HTML + PDF output to a subdirectory
  4. Sandbox must detect HTML/PDF, reject .pyc
  5. Retry scenario: re-run overwrites the same output files
  6. Handler sends only real artifacts, not cache
"""
from __future__ import annotations

import sys
import os
import time
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
from tools.sandbox import run_code, run_shell, _is_artifact_file


def _make_project_dir():
    """Create a realistic project dir under HOME with source files and output subdir."""
    d = Path(tempfile.mkdtemp(dir=config.HOST_HOME / "Desktop", prefix="test_igb_"))
    # Source files (like the iGB project)
    (d / "cli_report.py").write_text("# entry point\nimport mylib\nmylib.generate()")
    (d / "app").mkdir()
    (d / "app" / "__init__.py").write_text("")
    (d / "app" / "config.py").write_text("OUTPUT_DIR = 'app/output'")
    (d / "app" / "core").mkdir()
    (d / "app" / "core" / "__init__.py").write_text("")
    (d / "app" / "core" / "builder.py").write_text("def build(): pass")
    (d / "app" / "output").mkdir()
    return d


def _cleanup(d: Path):
    shutil.rmtree(d, ignore_errors=True)


class TestRealisticProjectExecution:
    """Simulates the exact iGB report generation scenario."""

    def test_fresh_run_detects_html_and_pdf(self):
        """First-ever run: script creates HTML + PDF in subdir. Both detected."""
        d = _make_project_dir()
        try:
            code = (
                "import os\n"
                "os.makedirs('app/output', exist_ok=True)\n"
                "with open('app/output/Light & Wonder iGB Report.html', 'w') as f:\n"
                "    f.write('<html>report</html>')\n"
                "with open('app/output/Light & Wonder iGB Report.pdf', 'wb') as f:\n"
                "    f.write(b'%PDF-fake')\n"
                "print('HTML saved: app/output/Light & Wonder iGB Report.html')\n"
                "print('PDF saved: app/output/Light & Wonder iGB Report.pdf')\n"
            )
            result = run_code(code, "python", working_dir=d, timeout=15)
            assert result.success, f"Execution failed: {result.stderr}"
            names = [Path(f).name for f in result.files_created]
            assert "Light & Wonder iGB Report.html" in names, f"HTML missing from: {names}"
            assert "Light & Wonder iGB Report.pdf" in names, f"PDF missing from: {names}"
            # No .pyc
            assert not any(n.endswith(".pyc") for n in names), f"pyc leaked: {names}"
        finally:
            _cleanup(d)

    def test_rerun_with_preexisting_output_detects_overwrite(self):
        """Output files exist from previous run. Script overwrites them. Still detected."""
        d = _make_project_dir()
        try:
            # Pre-existing output from a previous run
            (d / "app" / "output" / "Kambi iGB Report.html").write_text("<html>old</html>")
            (d / "app" / "output" / "Kambi iGB Report.pdf").write_bytes(b"%PDF-old")
            time.sleep(0.05)  # Ensure mtime difference

            code = (
                "with open('app/output/Kambi iGB Report.html', 'w') as f:\n"
                "    f.write('<html>new report</html>')\n"
                "with open('app/output/Kambi iGB Report.pdf', 'wb') as f:\n"
                "    f.write(b'%PDF-new')\n"
            )
            result = run_code(code, "python", working_dir=d, timeout=15)
            assert result.success
            names = [Path(f).name for f in result.files_created]
            assert "Kambi iGB Report.html" in names, f"Overwritten HTML missing: {names}"
            assert "Kambi iGB Report.pdf" in names, f"Overwritten PDF missing: {names}"
        finally:
            _cleanup(d)

    def test_retry_scenario_detects_output_after_failed_attempts(self):
        """
        Simulates 3 retries (each is a separate run_code call, like the real executor):
        - Retry 1: imports modules (creates .pyc), fails before writing output
        - Retry 2: same
        - Retry 3: succeeds, writes HTML + PDF
        """
        d = _make_project_dir()
        try:
            # Create a real importable module
            (d / "mylib.py").write_text(
                "def generate():\n"
                "    with open('app/output/Report.html', 'w') as f:\n"
                "        f.write('<html>done</html>')\n"
                "    with open('app/output/Report.pdf', 'wb') as f:\n"
                "        f.write(b'%PDF-done')\n"
            )

            # Retry 1: imports module (creates .pyc) but fails
            r1 = run_code(
                "import mylib\nraise RuntimeError('bad entry point')",
                "python", working_dir=d, timeout=15,
            )
            assert not r1.success  # Expected to fail
            assert not any(Path(f).name.endswith(".pyc") for f in r1.files_created)

            time.sleep(0.05)

            # Retry 2: same failure
            r2 = run_code(
                "import mylib\nraise RuntimeError('still wrong')",
                "python", working_dir=d, timeout=15,
            )
            assert not r2.success

            time.sleep(0.05)

            # Retry 3: success — writes output
            r3 = run_code(
                "import mylib\nmylib.generate()\nprint('done')",
                "python", working_dir=d, timeout=15,
            )
            assert r3.success, f"Retry 3 failed: {r3.stderr}"
            names = [Path(f).name for f in r3.files_created]
            assert "Report.html" in names, f"HTML missing after retry 3: {names}"
            assert "Report.pdf" in names, f"PDF missing after retry 3: {names}"
            assert not any(n.endswith(".pyc") for n in names), f"pyc leaked: {names}"
        finally:
            _cleanup(d)

    def test_run_code_with_module_imports_filters_pyc(self):
        """run_code: script imports multiple modules. Only real output returned."""
        d = _make_project_dir()
        try:
            (d / "mod_a.py").write_text("A = 1")
            (d / "mod_b.py").write_text("B = 2")
            (d / "mod_c.py").write_text("C = 3")

            result = run_code(
                "import mod_a, mod_b, mod_c\n"
                "with open('result.csv', 'w') as f:\n"
                "    f.write('a,b,c')\n"
                "print('done')",
                "python", working_dir=d, timeout=15,
            )
            assert result.success
            names = [Path(f).name for f in result.files_created]
            assert "result.csv" in names
            assert not any(n.endswith(".pyc") for n in names), f"pyc in: {names}"
            assert not any("__pycache__" in f for f in result.files_created)
        finally:
            _cleanup(d)

    def test_handler_artifact_filter_logic(self):
        """Simulate what handlers.py does: only send existing files under size limit."""
        d = _make_project_dir()
        try:
            html = d / "app" / "output" / "Report.html"
            html.write_text("<html>" + "x" * 1000 + "</html>")
            pdf = d / "app" / "output" / "Report.pdf"
            pdf.write_bytes(b"%PDF" + b"\x00" * 500)
            gone = d / "app" / "output" / "deleted.txt"  # doesn't exist

            artifacts = [str(html), str(pdf), str(gone)]

            # Replicate handler logic
            sent = []
            skipped = []
            for fpath in artifacts:
                p = Path(fpath)
                if not p.is_file():
                    skipped.append(f"not found: {fpath}")
                elif p.stat().st_size >= config.MAX_FILE_SIZE_BYTES:
                    skipped.append(f"too large: {fpath}")
                else:
                    sent.append(p.name)

            assert "Report.html" in sent
            assert "Report.pdf" in sent
            assert len(skipped) == 1
            assert "deleted.txt" in skipped[0]
        finally:
            _cleanup(d)

    def test_preexisting_pyc_and_ds_store_never_leak(self):
        """Even if .pyc and .DS_Store are modified during execution, they're filtered."""
        d = _make_project_dir()
        try:
            # Pre-existing cache
            cache_dir = d / "__pycache__"
            cache_dir.mkdir()
            (cache_dir / "old.cpython-311.pyc").write_bytes(b"\x00" * 100)
            (d / ".DS_Store").write_bytes(b"\x00" * 50)
            time.sleep(0.05)

            # Script that touches cache AND creates real output
            code = (
                "import os\n"
                "# Python will update __pycache__ on import\n"
                "os.makedirs('__pycache__', exist_ok=True)\n"
                "with open('__pycache__/new.cpython-311.pyc', 'wb') as f:\n"
                "    f.write(b'fake pyc')\n"
                "with open('.DS_Store', 'wb') as f:\n"
                "    f.write(b'updated')\n"
                "with open('output.json', 'w') as f:\n"
                "    f.write('{\"result\": true}')\n"
            )
            result = run_code(code, "python", working_dir=d, timeout=15)
            assert result.success
            names = [Path(f).name for f in result.files_created]
            assert "output.json" in names
            assert "old.cpython-311.pyc" not in names
            assert "new.cpython-311.pyc" not in names
            assert ".DS_Store" not in names
        finally:
            _cleanup(d)
