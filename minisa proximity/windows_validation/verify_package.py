"""Verify transferred files and both firmware manifests without touching USB."""
import hashlib
import json
from pathlib import Path


def verify_files(root):
    root = root.resolve()
    manifest = json.loads((root / "package-manifest.json").read_text(encoding="utf-8"))
    errors = []
    for relative, expected in manifest["files"].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            errors.append(f"Unsafe manifest path: {relative}")
        elif not path.is_file():
            errors.append(f"Missing: {relative}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
            errors.append(f"Changed/corrupt: {relative}")
    return errors


def main():
    from flash_firmware import FIRMWARE_ROOT, verified_image
    root = Path(__file__).resolve().parent
    errors = verify_files(root)
    for role in ("scanner", "beacon"):
        try:
            verified_image(role, FIRMWARE_ROOT / role)
        except (OSError, ValueError, KeyError) as error:
            errors.append(f"{role}: {error}")
    if errors:
        raise SystemExit("Package verification failed:\n" + "\n".join(errors))
    print("PASS: packaged files and both source/image manifests verified. No USB access performed.")


if __name__ == "__main__":
    main()
