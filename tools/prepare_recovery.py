"""Apply the Crumpet recovery-core patch, without overwriting unrelated changes."""
import argparse
from pathlib import Path
import subprocess


def prepare(recovery):
    patch = Path(__file__).resolve().parents[1] / "crumpet/patches/recovery-nand.patch"
    command = ["git", "-C", str(recovery), "apply"]
    applied = subprocess.run([*command, "--reverse", "--check", str(patch)], capture_output=True)
    if applied.returncode == 0:
        print("Crumpet recovery patch is already applied.")
        return
    subprocess.run([*command, "--check", str(patch)], check=True)
    subprocess.run([*command, str(patch)], check=True)
    print("Applied Crumpet NAND/UBIFS support. Rebuild recovery from a clean ramdisk output.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recovery", type=Path, help="Path to bootable/recovery")
    args = parser.parse_args()
    prepare(args.recovery.resolve())
