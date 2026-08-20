"""Materialize the official embedded Ohana icon for Windows build metadata."""

import argparse
import base64
from pathlib import Path

from ohana_katsuyu.icon_data import OFFICIAL_OHANA_ICON_BASE64


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    output = parser.parse_args().output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(base64.b64decode(OFFICIAL_OHANA_ICON_BASE64))


if __name__ == "__main__":
    main()
