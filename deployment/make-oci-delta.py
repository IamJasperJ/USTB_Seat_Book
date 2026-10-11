"""Make a Docker 29 OCI update bundle, omitting blobs already on the target.

Input: newline-separated SHA-256 blob names, collected read-only from the target.
The full archive stays on the build host for fallback. No production data is read.
"""
import pathlib
import re
import sys
import tarfile

known = set(sys.stdin.read().split())
if not known or any(not re.fullmatch(r"[0-9a-f]{64}", x) for x in known):
    raise ValueError("Expected target blob digest inventory")
source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
kept = omitted = 0
with tarfile.open(source, "r:") as src, tarfile.open(target, "w:gz") as dst:
    for item in src:
        name = item.name.removeprefix("./")
        # Force OCI import; legacy Docker manifest needs all layer archive paths.
        if name == "manifest.json":
            continue
        if name.startswith("blobs/sha256/") and name.split("/")[-1] in known:
            omitted += item.size
            continue
        if item.isfile() and name.startswith("blobs/sha256/"):
            import hashlib
            data = src.extractfile(item).read()
            if hashlib.sha256(data).hexdigest() != name.split("/")[-1]:
                raise ValueError("Invalid source blob digest")
            import io
            dst.addfile(item, io.BytesIO(data))
        else:
            dst.addfile(item, src.extractfile(item) if item.isfile() else None)
        kept += item.size
print("delta_bytes", target.stat().st_size, "included_bytes", kept, "omitted_bytes", omitted)
