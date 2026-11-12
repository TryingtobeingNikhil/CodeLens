"""Build the standalone backend for the current OS into bin/codelens-backend/.

Run from the repo root, in an environment with the backend and PyInstaller installed:
    pip install -e . pyinstaller
    python packaging/build_backend.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "bin"
WORK = ROOT / "build" / "pyinstaller"

# Packages that carry native libraries or are imported dynamically
COLLECT = [
    "sqlite_vec",
    "tree_sitter",
    "tree_sitter_python",
    "tree_sitter_typescript",
    "tree_sitter_javascript",
    "tree_sitter_go",
    "tree_sitter_rust",
    "tree_sitter_java",
    "tree_sitter_cpp",
]
HIDDEN = ["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
          "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on"]


def main() -> None:
    shutil.rmtree(DIST / "codelens-backend", ignore_errors=True)
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir",
        "--name", "codelens-backend",
        "--distpath", str(DIST), "--workpath", str(WORK), "--specpath", str(WORK),
        "--paths", str(ROOT),
    ]
    for pkg in COLLECT:
        cmd += ["--collect-all", pkg]
    for mod in HIDDEN:
        cmd += ["--hidden-import", mod]
    cmd.append(str(ROOT / "packaging" / "codelens_backend.py"))
    subprocess.run(cmd, check=True, cwd=ROOT)
    print(f"Built {DIST / 'codelens-backend'}")


if __name__ == "__main__":
    main()
