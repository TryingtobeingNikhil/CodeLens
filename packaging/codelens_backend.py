"""Entry point for the standalone (PyInstaller) backend shipped inside the VSIX.

Usage: codelens-backend [--host 127.0.0.1] [--port 8000]
"""
import argparse
import os

import uvicorn

from backend.main import app


def main() -> None:
    parser = argparse.ArgumentParser(prog="codelens-backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("CODELENS_PORT", "8000")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
