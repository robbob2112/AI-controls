"""Browse and install single-file GGUF releases from public catalogs."""

import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
import urllib.parse
import urllib.request

SOURCES = {"modelscope", "github"}
MAX_BYTES = 64_000_000_000
PART = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}"
FAMILY = re.compile(f"{PART}/{PART}")
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,195}[.]gguf", re.I)
ALLOWED = {
    "api.github.com",
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "modelscope.ai",
    "modelscope.cn",
    "www.modelscope.cn",
}


class CatalogRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urllib.parse.urlsplit(newurl)
        if (
            url.scheme != "https"
            or url.port not in (None, 443)
            or url.username
            or not (
                url.hostname in ALLOWED
                or (url.hostname or "").endswith(
                    (".modelscope.cn", ".modelscope.ai", ".aliyuncs.com")
                )
            )
        ):
            raise ValueError("Catalog redirected to an unsupported download host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(CatalogRedirect())


def public_read(url, limit=2_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": "AI Controls model catalog"})
    with OPENER.open(req, timeout=7) as response:
        return response.read(limit).decode("utf-8")


def query(values):
    return urllib.parse.urlencode(values)


def ms_file(family, filename, revision="master"):
    return (
        "https://modelscope.ai/api/v1/models/"
        + family
        + "/repo?"
        + query({"Revision": revision, "FilePath": filename})
    )


def repositories(source, term):
    # A known repository can be opened even if it does not rank in catalog search.
    term = term.strip()
    if FAMILY.fullmatch(term) and ".." not in term:
        return [(term, "Public model repository")]
    if source == "modelscope":
        data = json.loads(
            public_read(
                "https://modelscope.cn/openapi/v1/models?"
                + query(
                    {"search": ("GGUF" if not term else term), "page_size": 35, "sort": "downloads"}
                )
            )
        )
        return [
            (x["id"], x.get("description") or " ".join(x.get("tasks", [])))
            for x in data["data"]["models"]
            if not x.get("gated")
            and not x.get("private")
            and "gguf" in (x["id"] + " " + " ".join(x.get("tags", []))).lower()
        ]
    data = json.loads(
        public_read(
            "https://api.github.com/search/repositories?"
            + query({"q": (term + " GGUF in:name").strip(), "per_page": 25, "sort": "stars"})
        )
    )
    return [(x["full_name"], x.get("description") or "") for x in data["items"]]


def model_files(source, family):
    if not FAMILY.fullmatch(family) or ".." in family:
        raise ValueError("Invalid model repository")
    if source == "modelscope":
        files = json.loads(
            public_read(f"https://modelscope.ai/api/v1/models/{family}/repo/files?Revision=master")
        )["Data"]["Files"]
        return [
            (
                x["Name"],
                x.get("Size"),
                x.get("Sha256"),
                ms_file(family, x["Path"], x.get("Revision") or "master"),
                f"https://modelscope.ai/models/{family}",
            )
            for x in files
            if x.get("Path") == x.get("Name") and x.get("Type") == "blob"
        ]
    releases = json.loads(
        public_read(f"https://api.github.com/repos/{family}/releases?per_page=15")
    )
    return [
        (
            asset["name"],
            asset["size"],
            asset.get("digest"),
            asset["browser_download_url"],
            release["html_url"],
        )
        for release in releases
        if not release.get("draft")
        for asset in release.get("assets", [])
    ]


def selected_file(source, family, key):
    for asset in assets(source, family):
        if asset["name"] == key:
            return asset
    raise ValueError("This GGUF file is no longer available in the catalog")


def assets(source, family):
    result = {}
    for filename, size, digest, url, page in model_files(source, family):
        if (
            not FILENAME.fullmatch(filename)
            or "mmproj" in filename.lower()
            or re.search(r"-\d{5}-of-\d{5}", filename)
            or type(size) is not int
            or not 4 <= size <= MAX_BYTES
        ):
            continue
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED or parsed.username:
            continue
        fingerprint = hashlib.sha256(url.encode()).hexdigest()[:12]
        slug = re.sub(r"[^a-z0-9]+", "-", family.lower()).strip("-")[:38]
        variant = re.sub(r"[^a-z0-9]+", "-", filename[:-5].lower()).strip("-")[:40]
        alias = f"{source}-{slug}:{variant}-{fingerprint}"
        result[alias] = {
            "name": alias,
            "label": filename,
            "size": size,
            "digest": digest,
            "url": page,
            "download": url,
            "family": family,
        }
    return list(result.values())[:100]


def lookup(source, term, family, purpose):
    if not family:
        return [
            {"name": name, "family": True, "purpose": purpose(name, hints), "specs": hints[:300]}
            for name, hints in repositories(source, term)
            if FAMILY.fullmatch(name) and ".." not in name
        ]
    return [
        {
            "name": model["name"],
            "label": model["label"],
            "url": model["url"],
            "specs": f"{model['size']/2**30:.2f} GiB GGUF",
            "purpose": purpose(model["label"]),
            "source_file": {"source": source, "family": family},
        }
        for model in assets(source, family)
    ]


def card(source, family):
    if not FAMILY.fullmatch(family) or ".." in family:
        raise ValueError("Invalid model repository")
    if source == "modelscope":
        return public_read(ms_file(family, "README.md"), 16000)
    releases = json.loads(public_read(f"https://api.github.com/repos/{family}/releases?per_page=5"))
    return "\n\n".join(x.get("body") or "" for x in releases)[:16000]


def install(source, family, alias, store, ollama_open, progress):
    asset = selected_file(source, family, alias)
    if shutil.disk_usage(store).free < asset["size"] * 2 + 1_000_000_000:
        raise RuntimeError("Insufficient disk space to stage and import this model")
    expected = asset["size"]
    checksum = hashlib.sha256()
    count = 0
    with tempfile.TemporaryFile(dir=store) as staged:
        request = urllib.request.Request(
            asset["download"], headers={"User-Agent": "AI Controls model importer"}
        )
        with OPENER.open(request, timeout=30) as response:
            chunk = response.read(4)
            if chunk != b"GGUF":
                raise RuntimeError("Download is not a GGUF file")
            staged.write(chunk)
            checksum.update(chunk)
            count = len(chunk)
            while chunk := response.read(4 * 1024 * 1024):
                count += len(chunk)
                if count > expected:
                    raise RuntimeError("Download exceeded its published size")
                staged.write(chunk)
                checksum.update(chunk)
                progress("downloading", count, expected)
        digest = checksum.hexdigest()
        supplied = (asset["digest"] or "").removeprefix("sha256:")
        if count != expected or (supplied and supplied.lower() != digest):
            raise RuntimeError("Downloaded file did not match its published size or checksum")
        staged.seek(0)
        progress("registering", count, expected)
        upload = urllib.request.Request(
            "http://127.0.0.1:11449/api/blobs/sha256:" + digest,
            data=staged,
            headers={"Content-Length": str(count), "Content-Type": "application/octet-stream"},
            method="POST",
        )
        with urllib.request.urlopen(upload, timeout=180):
            pass
    with ollama_open(
        11449,
        "/api/create",
        {"model": alias, "files": {asset["label"]: "sha256:" + digest}, "stream": False},
        timeout=180,
    ) as response:
        result = json.load(response)
    if result.get("status") != "success":
        raise RuntimeError(result.get("error") or "Ollama did not register the GGUF")
