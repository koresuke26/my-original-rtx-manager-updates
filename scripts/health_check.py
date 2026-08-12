from __future__ import annotations

import ast
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

REPOSITORY = "koresuke26/my-original-rtx-manager-updates"
APP_FILE = "My Original RTX Manager.pyw"
LATEST_FILE = "latest.json"


def fail(message: str) -> None:
    print(f"::error::{message}")
    raise SystemExit(1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_constants(source: str) -> dict[str, object]:
    tree = ast.parse(source, filename=APP_FILE)
    wanted = {
        "APP_VERSION_NUMBER",
        "PACK_NUMBER",
        "PACK_FOLDER",
        "PACK_NAME",
        "UPDATE_REPOSITORY",
    }
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in wanted:
                try:
                    found[name] = ast.literal_eval(node.value)
                except Exception:
                    pass
    return found


def validate_raw_url(url: str, expected_file: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com":
        fail(f"Invalid update URL: {url}")
    expected_path = f"/{REPOSITORY}/main/{expected_file}"
    if unquote(parsed.path) != expected_path:
        fail(f"Update URL points to the wrong file: {url}")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    app_path = root / APP_FILE
    latest_path = root / LATEST_FILE

    if not app_path.is_file():
        fail(f"Missing {APP_FILE}")
    if not latest_path.is_file():
        fail(f"Missing {LATEST_FILE}")

    source = app_path.read_text(encoding="utf-8-sig")
    compile(source, APP_FILE, "exec")
    print("OK: Python syntax")

    for marker in ("<<<<<<<", "=======", ">>>>>>>"):
        if marker in source:
            fail(f"Merge conflict marker found: {marker}")
    print("OK: No merge conflict markers")

    latest = json.loads(latest_path.read_text(encoding="utf-8-sig"))
    if latest.get("schema_version") != 1:
        fail("latest.json schema_version must be 1")
    if latest.get("repository") != REPOSITORY:
        fail("latest.json repository mismatch")

    app = latest.get("app")
    pack = latest.get("pack")
    if not isinstance(app, dict) or not isinstance(pack, dict):
        fail("latest.json requires app and pack objects")

    constants = read_constants(source)
    expected_constants = {
        "APP_VERSION_NUMBER": str(app.get("version", "")),
        "PACK_NUMBER": pack.get("number"),
        "PACK_FOLDER": pack.get("folder"),
        "PACK_NAME": pack.get("name"),
        "UPDATE_REPOSITORY": REPOSITORY,
    }
    for name, expected in expected_constants.items():
        actual = constants.get(name)
        if actual != expected:
            fail(f"{name} mismatch: app={actual!r}, latest.json={expected!r}")
    print("OK: App constants match latest.json")

    if app.get("file_name") != APP_FILE:
        fail("latest.json app.file_name mismatch")
    validate_raw_url(str(app.get("url", "")), APP_FILE)

    actual_app_sha = sha256(app_path)
    if str(app.get("sha256", "")).lower() != actual_app_sha:
        fail(f"App SHA256 mismatch: actual={actual_app_sha}")
    print("OK: App SHA256")

    pack_file = str(pack.get("file_name", ""))
    if not pack_file or Path(pack_file).name != pack_file or not pack_file.lower().endswith(".mcpack"):
        fail("Invalid pack.file_name")
    pack_path = root / pack_file
    if not pack_path.is_file():
        fail(f"Missing pack file: {pack_file}")
    validate_raw_url(str(pack.get("url", "")), pack_file)

    actual_pack_sha = sha256(pack_path)
    if str(pack.get("sha256", "")).lower() != actual_pack_sha:
        fail(f"Pack SHA256 mismatch: actual={actual_pack_sha}")
    print("OK: Pack SHA256")

    with zipfile.ZipFile(pack_path) as archive:
        bad = archive.testzip()
        if bad:
            fail(f"Corrupt file inside mcpack: {bad}")
        if "manifest.json" not in archive.namelist():
            fail("manifest.json missing from mcpack root")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8-sig"))

    header = manifest.get("header", {}) if isinstance(manifest, dict) else {}
    actual_uuid = str(header.get("uuid", "")).lower()
    expected_uuid = str(pack.get("header_uuid", "")).lower()
    if not expected_uuid or actual_uuid != expected_uuid:
        fail(f"Pack UUID mismatch: actual={actual_uuid}, expected={expected_uuid}")

    manifest_version = header.get("version")
    if isinstance(manifest_version, list):
        manifest_version = ".".join(str(x) for x in manifest_version)
    if str(pack.get("version", "")) and str(manifest_version) != str(pack.get("version")):
        fail(f"Pack version mismatch: actual={manifest_version}, expected={pack.get('version')}")

    print("OK: mcpack integrity, UUID and version")
    print("HEALTH CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
