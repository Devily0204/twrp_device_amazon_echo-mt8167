"""Inspect the stock eraseblock-aligned redundant BCB records in a misc backup."""
import argparse
import json
from pathlib import Path


def records(path):
    found = []
    with path.open("rb") as stream:
        for block in range(8):
            stream.seek(block * 262144 + 0x360)
            metadata = stream.read(8)
            if metadata[:5] != b"\0ABB\1":
                continue
            slots = [{"priority": b & 15, "tries": (b >> 4) & 7, "successful": bool(b & 128)} for b in metadata[5:7]]
            found.append({"block": block, "offset": block * 262144, "metadata": metadata.hex(), "slots": slots})
    return found


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("misc", type=Path)
    print(json.dumps(records(parser.parse_args().misc), indent=2))
