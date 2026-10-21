"""Host-only OOM priority guard, scoped to the exact live booking container."""
import os
from pathlib import Path
import re
import subprocess


def main():
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}} {{.State.Running}}", "ustb-seat-book"],
        capture_output=True, text=True, timeout=5,
    )
    parts = result.stdout.split()
    if result.returncode or len(parts) != 2 or parts[1] != "true":
        return
    cid = parts[0]
    if not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise RuntimeError("Invalid container identity")
    failures = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        directory = score = None
        try:
            # Directory descriptors prevent accidentally following a reused PID.
            directory = os.open(str(entry), os.O_RDONLY | os.O_DIRECTORY)
            group = os.open("cgroup", os.O_RDONLY, dir_fd=directory)
            with os.fdopen(group) as handle:
                if cid not in handle.read():
                    continue
            score = os.open("oom_score_adj", os.O_RDWR, dir_fd=directory)
            if os.read(score, 64).strip() != b"-500":
                os.lseek(score, 0, os.SEEK_SET)
                os.write(score, b"-500\n")
        except ProcessLookupError:
            pass
        except FileNotFoundError:
            pass
        except PermissionError:
            failures += 1
        finally:
            if score is not None:
                os.close(score)
            if directory is not None:
                os.close(directory)
    if failures:
        raise RuntimeError(f"Could not protect {failures} booking processes")


if __name__ == "__main__":
    main()
