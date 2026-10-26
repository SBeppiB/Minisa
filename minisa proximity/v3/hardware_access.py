"""Read-only discovery; candidate VID/PID never establishes a board's role."""
import json
import os
import subprocess


def serial_devices():
    from serial.tools import list_ports
    return [dict(port=p.device, description=p.description, hwid=p.hwid,
                 vid=p.vid, pid=p.pid, serial_number=p.serial_number,
                 manufacturer=p.manufacturer, location=p.location,
                 candidate=p.vid in (0x303A, 0x1A86, 0x10C4, 0x0403))
            for p in list_ports.comports()]


def diagnose():
    report = {"serial_devices": serial_devices(), "methods": [],
              "interpretation": "An empty port list alone does not prove physical absence."}
    if os.name == "nt":
        commands = [
            ["pnputil", "/enum-devices", "/connected", "/class", "Ports"],
            ["pnputil", "/enum-devices", "/disconnected", "/class", "Ports"],
            ["pnputil", "/enum-devices", "/connected", "/bus", "USB"],
            ["pnputil", "/enum-devices", "/problem"],
            ["reg", "query", r"HKLM\HARDWARE\DEVICEMAP\SERIALCOMM"],
        ]
        for command in commands:
            try:
                result = subprocess.run(command, capture_output=True, text=True,
                                        errors="replace", timeout=20)
                report["methods"].append(dict(command=command, returncode=result.returncode,
                                              stdout=result.stdout, stderr=result.stderr))
            except (OSError, subprocess.TimeoutExpired) as error:
                report["methods"].append(dict(command=command, error=str(error)))
    return report


if __name__ == "__main__":
    print(json.dumps(diagnose(), indent=2))
