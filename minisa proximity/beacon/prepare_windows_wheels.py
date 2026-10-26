"""Prepare offline Windows x64 dependencies on the canonical build host.

Requires network/certificate access on this host only. Does not install packages
globally, alter TLS checks, access USB, or need the Minisa runtime environment.
"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
    wheels = ROOT / ".build/windows-wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    requirements = [line.strip() for line in (ROOT / "windows_validation/requirements.txt").read_text().splitlines()
                    if line.strip() and not line.startswith(("--", "#"))]
    pip = [sys.executable, "-m", "pip", "--cache-dir", str(ROOT / ".build/pip-cache")]
    # PyPI supplies esptool as an sdist; build its platform-independent wheel.
    subprocess.run(pip + ["wheel", "--no-deps", "--wheel-dir", str(wheels), "esptool==5.3.1"], check=True)
    for version in ("310", "311", "312", "313", "314"):
        subprocess.run(pip + ["download", "--find-links", str(wheels), "--only-binary=:all:",
            "--platform", "win_amd64", "--python-version", version, "--implementation", "cp",
            "--abi", "cp" + version, "--dest", str(wheels), *requirements], check=True)


if __name__ == "__main__":
    main()
