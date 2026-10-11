"""Fetch the official base with bounded range retries and digest verification."""
import concurrent.futures
import gzip
import hashlib
import io
import json
import pathlib
import tarfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent / "base-download"
ROOT.mkdir(exist_ok=True)
API = "https://mcr.microsoft.com/v2/playwright/python/"
TAG = "mcr.microsoft.com/playwright/python:v1.50.0-noble"
HEADERS = {"Accept": "application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json"}


def file_sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def request(path, headers=None):
    return urllib.request.urlopen(urllib.request.Request(API + path, headers=headers or HEADERS), timeout=60)


def fetch(desc):
    digest, size = desc["digest"], desc["size"]
    dst = ROOT / digest.split(":")[1]
    if dst.exists() and dst.stat().st_size == size:
        if file_sha(dst) == dst.name:
            return dst
    partial = dst.with_suffix(".part")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > size:
        raise RuntimeError("Partial exceeds expected blob size")
    if size > 100 * 1024 * 1024 and offset < size:
        def chunk(start):
            end = min(start + 4 * 1024 * 1024, size) - 1
            cached = ROOT / (dst.name + f".chunk-{start}")
            if cached.exists() and cached.stat().st_size == end - start + 1:
                return cached
            for attempt in range(5):
                try:
                    with request("blobs/" + digest, {"Range": f"bytes={start}-{end}"}) as r:
                        if r.status != 206 or not r.headers.get("Content-Range", "").startswith(f"bytes {start}-{end}/"):
                            raise RuntimeError("Incorrect chunk range")
                        data = r.read(end - start + 2)
                    if len(data) != end - start + 1:
                        raise RuntimeError("Incomplete chunk")
                    cached.write_bytes(data)
                    return cached
                except Exception:
                    if attempt == 4:
                        raise
                    time.sleep(min(2 ** attempt, 8))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for cached in pool.map(chunk, range(offset, size, 4 * 1024 * 1024)):
                data = cached.read_bytes()
                with partial.open("ab") as f:
                    f.write(data)
                offset += len(data)
                print(dst.name[:12], offset, "/", size, flush=True)
    while offset < size:
        end = min(offset + 4 * 1024 * 1024, size) - 1
        for attempt in range(5):
            try:
                with request("blobs/" + digest, {"Range": f"bytes={offset}-{end}"}) as r:
                    if r.status != 206 or not r.headers.get("Content-Range", "").startswith(f"bytes {offset}-{end}/"):
                        raise RuntimeError("Server did not honor exact range")
                    data = r.read(end - offset + 2)
                if len(data) != end - offset + 1:
                    raise RuntimeError("Incomplete range response")
                with partial.open("ab") as f:
                    f.write(data)
                offset += len(data)
                print(dst.name[:12], offset, "/", size, flush=True)
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(min(2 ** attempt, 8))
    actual = file_sha(partial)
    if actual != dst.name:
        raise RuntimeError("Blob digest mismatch: " + dst.name)
    partial.rename(dst)
    return dst


with request("manifests/v1.50.0-noble") as r:
    index = json.load(r)
selected = next(x for x in index["manifests"] if x["platform"]["architecture"] == "arm64" and x["platform"]["os"] == "linux")
with request("manifests/" + selected["digest"]) as r:
    raw = r.read()
assert "sha256:" + hashlib.sha256(raw).hexdigest() == selected["digest"]
manifest = json.loads(raw)
descs = [manifest["config"], *manifest["layers"]]
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    files = list(pool.map(fetch, descs))
config = json.loads(files[0].read_bytes())
assert config["architecture"] == "arm64"
layers = []
for i, blob in enumerate(files[1:]):
    dst = ROOT / f"layer-{i}.tar"
    with gzip.open(blob, "rb") as src, dst.open("wb") as out:
        import shutil
        shutil.copyfileobj(src, out, length=1024 * 1024)
    assert "sha256:" + file_sha(dst) == config["rootfs"]["diff_ids"][i]
    layers.append(dst)
archive = ROOT.parent / "arm64-base.tar"
with tarfile.open(archive, "w") as tar:
    tar.add(files[0], arcname="config.json")
    for layer in layers:
        tar.add(layer, arcname=layer.name)
    data = json.dumps([{"Config": "config.json", "RepoTags": [TAG], "Layers": [p.name for p in layers]}]).encode()
    info = tarfile.TarInfo("manifest.json")
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))
print("Verified ARM64 base archive:", archive, flush=True)
