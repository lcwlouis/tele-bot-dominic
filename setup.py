"""Write the Git revision into release wheels and source distributions."""

import subprocess
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist


def write_version(destination: str) -> None:
    root = Path(__file__).resolve().parent
    if not (root / ".git").exists():
        return  # a source distribution already carries its version file
    try:
        version = subprocess.check_output(
            ["git", "describe", "--always", "--tags", "--dirty"],
            cwd=root, stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return
    if version:
        target = Path(destination) / "dom" / "_version.py"
        # sdist can hardlink copied source files. Replace the link without changing the checkout.
        target.unlink(missing_ok=True)
        target.write_text(f"VERSION = {version!r}\n", encoding="utf-8")


class BuildPython(build_py):
    def run(self):
        super().run()
        if not self.editable_mode:
            write_version(self.build_lib)


class SourceDistribution(sdist):
    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        write_version(base_dir)


setup(cmdclass={"build_py": BuildPython, "sdist": SourceDistribution})
