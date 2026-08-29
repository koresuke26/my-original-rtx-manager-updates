from __future__ import annotations

import binascii
import ctypes
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_NAME = "My Original RTX Manager"
APP_VERSION_NUMBER = "2.8.1"
APP_VERSION = f"{APP_VERSION_NUMBER} Python"
PACK_NUMBER = 34
PACK_FOLDER = "My_Original_Visual_Pack_34"
PACK_NAME = "My Original Visual Pack！34 水・洞窟MAX版"
BASE_DIR = Path(__file__).resolve().parent
EMBEDDED_PACK = BASE_DIR / "assets" / "My_Original_Visual_Pack_34.mcpack"
UPDATE_REPOSITORY = "koresuke26/my-original-rtx-manager-updates"
UPDATE_RAW_ROOT = f"https://raw.githubusercontent.com/{UPDATE_REPOSITORY}/main"
UPDATE_MANIFEST_URL = f"{UPDATE_RAW_ROOT}/latest.json"
ALLOWED_UPDATE_HOSTS = {"raw.githubusercontent.com"}
MAX_MANIFEST_BYTES = 256 * 1024
MAX_APP_BYTES = 5 * 1024 * 1024
MAX_PACK_BYTES = 100 * 1024 * 1024

DEFAULT_GPU = "RTX 4060"
GPU_PROFILES = {
    "RTX 2060": {
        "fog": 25, "emissive": 90, "relief": 2, "density": 2,
        "mirror": 70, "roughness": 55, "water_transparency": 100,
        "ambient": True, "anti_flicker": True,
    },
    "RTX 3060": {
        "fog": 30, "emissive": 100, "relief": 3, "density": 2,
        "mirror": 80, "roughness": 50, "water_transparency": 100,
        "ambient": True, "anti_flicker": True,
    },
    "RTX 4060": {
        "fog": 35, "emissive": 110, "relief": 3, "density": 3,
        "mirror": 90, "roughness": 45, "water_transparency": 100,
        "ambient": True, "anti_flicker": True,
    },
    "RTX 5060": {
        "fog": 40, "emissive": 120, "relief": 4, "density": 4,
        "mirror": 95, "roughness": 40, "water_transparency": 100,
        "ambient": True, "anti_flicker": True,
    },
}
DEFAULTS = {
    **GPU_PROFILES[DEFAULT_GPU],
    "night_vision": True,
}

METAL_GEM_TOKENS = (
    "iron", "gold", "copper", "netherite", "anvil", "chain", "rail",
    "hopper", "cauldron", "lightning_rod", "heavy_core", "lodestone",
    "diamond", "emerald", "amethyst", "lapis", "redstone",
)


@dataclass
class PackInfo:
    name: str
    path: Path
    folder: str
    uuid: str
    version: list[int]
    location: str
    kind: str

    @property
    def label(self) -> str:
        name = self.name
        if "！34" in name:
            name = "最新版｜！34 水・洞窟MAX版"
        elif "！" in name:
            match = re.search(r"！\d+", name)
            if match:
                name = f"{match.group(0)} {name.split(match.group(0), 1)[1].strip()[:22]}"
        return f"{name} — {self.location}"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_keyframes(value: object) -> object:
    """Return a stable Bedrock time-of-day keyframe map when one is present."""
    if not isinstance(value, dict):
        return value
    normalized: dict[str, object] = {}
    for raw_key, item in value.items():
        try:
            time_of_day = float(raw_key)
        except (TypeError, ValueError):
            continue
        if 0.0 <= time_of_day <= 1.0:
            normalized[f"{time_of_day:.6f}"] = item
    if not normalized:
        return value
    return dict(sorted(normalized.items(), key=lambda pair: float(pair[0])))


def repair_lighting_settings(lighting: dict) -> dict:
    """Repair the 1.26 lighting file shape used by the old !34 build.

    Bedrock accepts the 1.21.80 lighting schema for the settings used here,
    including sun/moon time-of-day keyframes.  Keeping this compatibility
    version avoids the parser rejecting the file before registering its custom
    identifier, which then produces one error for every client biome.
    """
    if not isinstance(lighting, dict):
        lighting = {}
    lighting["format_version"] = "1.21.80"
    settings = lighting.setdefault("minecraft:lighting_settings", {})
    if not isinstance(settings, dict):
        settings = {}
        lighting["minecraft:lighting_settings"] = settings
    directional = settings.setdefault("directional_lights", {})
    if isinstance(directional, dict):
        orbital = directional.setdefault("orbital", {})
        if isinstance(orbital, dict):
            for body_name in ("sun", "moon"):
                body = orbital.get(body_name)
                if not isinstance(body, dict):
                    continue
                if "illuminance" in body:
                    body["illuminance"] = normalize_keyframes(body["illuminance"])
                if isinstance(body.get("color"), dict):
                    body["color"] = normalize_keyframes(body["color"])
            offset = orbital.get("orbital_offset_degrees")
            if isinstance(offset, dict):
                orbital["orbital_offset_degrees"] = normalize_keyframes(offset)
            elif not isinstance(offset, (int, float)):
                orbital["orbital_offset_degrees"] = 3.0
    return lighting


def repair_pack_lighting(pack_path: Path) -> bool:
    """Repair a pack's registered global lighting definition in place."""
    lighting_path = pack_path / "lighting" / "global.json"
    if not lighting_path.is_file():
        return False
    original = read_json(lighting_path)
    repaired = repair_lighting_settings(original)
    if repaired != original:
        write_json(lighting_path, repaired)
    return True


def safe_key(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value or "pack")


def version_tuple(value: object) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value))[:4]
    return tuple(int(part) for part in parts) or (0,)


def validate_update_url(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url))
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_UPDATE_HOSTS:
        raise ValueError("更新URLが許可されたGitHubアドレスではありません。")
    expected_prefix = f"/{UPDATE_REPOSITORY}/"
    if not parsed.path.startswith(expected_prefix):
        raise ValueError("別のGitHubリポジトリを指す更新URLは使用できません。")
    return str(url)


def fetch_url_bytes(url: str, maximum: int) -> bytes:
    validate_update_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION_NUMBER}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            final_url = response.geturl()
            validate_update_url(final_url)
            length = response.headers.get("Content-Length")
            if length and int(length) > maximum:
                raise ValueError("更新ファイルが想定サイズを超えています。")
            payload = response.read(maximum + 1)
    except urllib.error.URLError as error:
        raise ConnectionError(f"GitHubへ接続できません: {error.reason}") from error
    if len(payload) > maximum:
        raise ValueError("更新ファイルが想定サイズを超えています。")
    return payload


def validate_update_manifest(manifest: object) -> dict:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("更新情報の形式が正しくありません。")
    if manifest.get("repository") != UPDATE_REPOSITORY:
        raise ValueError("更新情報のリポジトリが一致しません。")
    for key in ("app", "pack"):
        item = manifest.get(key)
        if not isinstance(item, dict):
            raise ValueError(f"更新情報に{key}がありません。")
        validate_update_url(str(item.get("url", "")))
        digest = str(item.get("sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"{key}の安全確認コードが不正です。")
    if not str(manifest["app"].get("version", "")):
        raise ValueError("アプリのバージョンがありません。")
    if Path(str(manifest["app"].get("file_name", ""))).name != "My Original RTX Manager.pyw":
        raise ValueError("アプリの更新ファイル名が不正です。")
    if int(manifest["pack"].get("number", 0)) <= 0:
        raise ValueError("パック番号が不正です。")
    if not Path(str(manifest["pack"].get("file_name", ""))).name.lower().endswith(".mcpack"):
        raise ValueError("パックの更新ファイル名が不正です。")
    if not str(manifest["pack"].get("folder", "")):
        raise ValueError("パックの導入フォルダー名がありません。")
    return manifest


def fetch_update_manifest() -> dict:
    payload = fetch_url_bytes(UPDATE_MANIFEST_URL, MAX_MANIFEST_BYTES)
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("GitHubの更新情報を読み取れません。") from error
    return validate_update_manifest(value)


def download_verified_file(info: dict, destination: Path, maximum: int, progress=None) -> Path:
    url = validate_update_url(str(info["url"]))
    expected = str(info["sha256"]).lower()
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION_NUMBER}"})
    temporary = destination.with_name(destination.name + ".download")
    temporary.unlink(missing_ok=True)
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=30) as response, temporary.open("wb") as output:
            validate_update_url(response.geturl())
            length_text = response.headers.get("Content-Length")
            length = int(length_text) if length_text and length_text.isdigit() else 0
            if length > maximum:
                raise ValueError("更新ファイルが想定サイズを超えています。")
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise ValueError("更新ファイルが想定サイズを超えています。")
                digest.update(chunk)
                output.write(chunk)
                if progress and length:
                    progress(min(95, 10 + 80 * total / length), f"GitHubから取得しています… {total // 1024}KB")
    except urllib.error.URLError as error:
        temporary.unlink(missing_ok=True)
        raise ConnectionError(f"GitHubから更新を取得できません: {error.reason}") from error
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if digest.hexdigest() != expected:
        temporary.unlink(missing_ok=True)
        raise ValueError("更新ファイルの安全確認コードが一致しません。適用を中止しました。")
    os.replace(temporary, destination)
    return destination


def safe_extract_archive(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for info in archive.infolist():
        member = Path(info.filename.replace("\\", "/"))
        if member.is_absolute() or ".." in member.parts:
            raise ValueError(f"危険なZIP内パスを検出しました: {info.filename}")
        target = (destination / member).resolve()
        try:
            target.relative_to(root)
        except ValueError as error:
            raise ValueError(f"危険なZIP内パスを検出しました: {info.filename}") from error
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)


def install_pack_archive(pack_file: Path, preview: bool, folder_name: str, expected_uuid: str | None = None) -> Path:
    roots = [root for _, is_preview, root in minecraft_candidates() if is_preview == preview and root.is_dir()]
    if not roots:
        raise FileNotFoundError("Minecraftの保存場所が見つかりません。Minecraftを一度起動して終了してください。")
    with zipfile.ZipFile(pack_file) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f"リソースパックが壊れています: {bad}")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8-sig"))
        except Exception as error:
            raise ValueError("リソースパックのmanifest.jsonを確認できません。") from error
        header_uuid = str(manifest.get("header", {}).get("uuid", ""))
        if expected_uuid and header_uuid.lower() != expected_uuid.lower():
            raise ValueError("更新パックのUUIDが更新情報と一致しません。")
        destination_root = roots[0] / "development_resource_packs"
        destination = destination_root / safe_key(folder_name)
        destination_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="my_original_rtx_install_") as temporary:
            stage = Path(temporary) / safe_key(folder_name)
            safe_extract_archive(archive, stage)
            repair_pack_lighting(stage)
            if destination.exists():
                backup = manager_home() / "Backups" / "Reinstall" / f"{int(time.time())}_{safe_key(folder_name)}"
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(destination, backup)
                shutil.rmtree(destination)
            shutil.copytree(stage, destination)
    return destination


def apply_python_update(downloaded: Path, version: str) -> Path:
    source = downloaded.read_text(encoding="utf-8-sig")
    compile(source, downloaded.name, "exec")
    if f'APP_NAME = "{APP_NAME}"' not in source:
        raise ValueError("取得したPythonファイルは対象アプリではありません。")
    application = Path(__file__).resolve()
    backup = manager_home() / "AppBackups" / f"{int(time.time())}_{safe_key(version)}_{application.name}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(application, backup)
    replacement = application.with_name(application.name + ".new")
    replacement.write_bytes(downloaded.read_bytes())
    try:
        os.replace(replacement, application)
    except Exception:
        replacement.unlink(missing_ok=True)
        raise
    return backup


def minecraft_candidates() -> list[tuple[str, bool, Path]]:
    candidates: list[tuple[str, bool, Path]] = []
    appdata = os.environ.get("APPDATA", "")
    local = os.environ.get("LOCALAPPDATA", "")
    if appdata:
        root = Path(appdata)
        candidates.extend(
            [
                ("Minecraft 通常版（現在）", False, root / "Minecraft Bedrock" / "users" / "shared" / "games" / "com.mojang"),
                ("Minecraft Preview（現在）", True, root / "Minecraft Bedrock Preview" / "users" / "shared" / "games" / "com.mojang"),
            ]
        )
    if local:
        packages = Path(local) / "Packages"
        candidates.extend(
            [
                ("Minecraft 通常版（旧UWP）", False, packages / "Microsoft.MinecraftUWP_8wekyb3d8bbwe" / "LocalState" / "games" / "com.mojang"),
                ("Minecraft Preview（旧UWP）", True, packages / "Microsoft.MinecraftWindowsBeta_8wekyb3d8bbwe" / "LocalState" / "games" / "com.mojang"),
            ]
        )
    return candidates


def manager_home() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_NAME


def baseline_path(pack: PackInfo) -> Path:
    return manager_home() / "Backups" / safe_key(pack.uuid) / "baseline"


def scan_pack_directory(root: Path, location: str, kind: str, issues: list[tuple[Path, str]]) -> list[PackInfo]:
    found: list[PackInfo] = []
    if not root.is_dir():
        return found
    try:
        entries = list(root.iterdir())
    except OSError as error:
        issues.append((root, str(error)))
        return found
    for folder in entries:
        if not folder.is_dir() or not (folder / "manifest.json").is_file():
            continue
        try:
            manifest = read_json(folder / "manifest.json")
            header = manifest.get("header", {})
            name = str(header.get("name") or folder.name)
            capabilities = manifest.get("capabilities", [])
            if not isinstance(capabilities, list):
                capabilities = []
            is_pbr = "pbr" in capabilities or "raytraced" in capabilities or re.search(r"RTX|Visual|PBR", name, re.I)
            if not is_pbr:
                continue
            found.append(
                PackInfo(
                    name=name,
                    path=folder,
                    folder=folder.name,
                    uuid=str(header.get("uuid") or folder),
                    version=list(header.get("version") or [0, 0, 0]),
                    location=location,
                    kind=kind,
                )
            )
        except Exception as error:
            issues.append((folder / "manifest.json", str(error)))
    return found


def ensure_baseline(pack: PackInfo) -> Path:
    baseline = baseline_path(pack)
    if (baseline / "manifest.json").is_file():
        return baseline
    baseline.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(pack.path, baseline)
    return baseline


def restore_baseline(pack: PackInfo) -> None:
    baseline = baseline_path(pack)
    if not (baseline / "manifest.json").is_file():
        raise FileNotFoundError("このパックのバックアップはまだありません。先に一度調整を適用してください。")
    if pack.path.exists():
        shutil.rmtree(pack.path)
    shutil.copytree(baseline, pack.path)


def copy_relief_preset(pack_path: Path, level: int) -> None:
    source = pack_path / "subpacks" / f"relief_{level}" / "textures" / "blocks"
    destination = pack_path / "textures" / "blocks"
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for item in source.rglob("*"):
            if item.is_file():
                target = destination / item.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)


def png_chunk(name: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + name + data + struct.pack(">I", binascii.crc32(name + data) & 0xFFFFFFFF)


def paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def read_png(path: Path) -> tuple[int, int, int, list[bytearray]]:
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"PNGではありません: {path.name}")
    offset = 8
    idat = bytearray()
    width = height = color_type = bit_depth = interlace = 0
    while offset < len(raw):
        length = struct.unpack(">I", raw[offset:offset + 4])[0]
        kind = raw[offset + 4:offset + 8]
        data = raw[offset + 8:offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", data)
        elif kind == b"IDAT":
            idat.extend(data)
        elif kind == b"IEND":
            break
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if bit_depth != 8 or interlace != 0 or channels is None:
        raise ValueError(f"未対応のPNG形式です: {path.name}")
    decoded = zlib.decompress(bytes(idat))
    stride = width * channels
    rows: list[bytearray] = []
    cursor = 0
    previous = bytearray(stride)
    for _ in range(height):
        filter_type = decoded[cursor]
        cursor += 1
        scan = bytearray(decoded[cursor:cursor + stride])
        cursor += stride
        for index in range(stride):
            left = scan[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                scan[index] = (scan[index] + left) & 255
            elif filter_type == 2:
                scan[index] = (scan[index] + up) & 255
            elif filter_type == 3:
                scan[index] = (scan[index] + ((left + up) // 2)) & 255
            elif filter_type == 4:
                scan[index] = (scan[index] + paeth(left, up, upper_left)) & 255
            elif filter_type != 0:
                raise ValueError(f"未対応のPNGフィルターです: {filter_type}")
        rows.append(scan)
        previous = scan
    return width, height, color_type, rows


def write_png(path: Path, width: int, height: int, color_type: int, rows: list[bytearray]) -> None:
    payload = b"".join(b"\x00" + bytes(row) for row in rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr) + png_chunk(b"IDAT", zlib.compress(payload, 9)) + png_chunk(b"IEND", b"")
    )


def tune_heightmap(path: Path, density: int, anti_flicker: bool) -> None:
    width, height, color_type, rows = read_png(path)
    if color_type != 0:
        raise ValueError(f"高さマップがグレースケールではありません: {path.name}")
    original = [bytearray(row) for row in rows]
    threshold = (5 - density) * 7
    for y in range(height):
        for x in range(width):
            value = original[y][x]
            if abs(value - 128) <= threshold:
                value = 128
            if anti_flicker:
                values = [original[(y + oy) % height][(x + ox) % width] for oy in (-1, 0, 1) for ox in (-1, 0, 1)]
                value = round(value * 0.82 + (sum(values) / len(values)) * 0.18)
            rows[y][x] = max(0, min(255, value))
    write_png(path, width, height, color_type, rows)


def material_targets(settings: dict, metal: bool) -> tuple[int, int]:
    global_roughness = round(settings["roughness"] * 2.55)
    roughness = round(global_roughness * 0.35) if metal else global_roughness
    if settings["anti_flicker"]:
        roughness = max(18 if metal else 82, roughness)
    metalness = round(settings["mirror"] * 2.55) if metal else 0
    return max(0, min(255, metalness)), max(0, min(255, roughness))


def decode_tga(path: Path) -> tuple[bytearray, bytearray, int]:
    raw = bytearray(path.read_bytes())
    if len(raw) < 18:
        raise ValueError(f"壊れたTGAです: {path.name}")
    id_length, color_map_type, image_type = raw[0], raw[1], raw[2]
    color_map_length = raw[5] | (raw[6] << 8)
    color_map_depth = raw[7]
    width, height, bpp = raw[12] | (raw[13] << 8), raw[14] | (raw[15] << 8), raw[16]
    bytes_per_pixel = bpp // 8
    if color_map_type != 0 or image_type not in (2, 10) or bytes_per_pixel not in (3, 4):
        raise ValueError(f"未対応のTGA形式です: {path.name}")
    prefix_length = 18 + id_length + color_map_length * ((color_map_depth + 7) // 8)
    offset = prefix_length
    total = width * height * bytes_per_pixel
    pixels = bytearray()
    if image_type == 2:
        pixels.extend(raw[offset:offset + total])
    else:
        while len(pixels) < total:
            packet = raw[offset]
            offset += 1
            count = (packet & 0x7F) + 1
            if packet & 0x80:
                pixel = raw[offset:offset + bytes_per_pixel]
                offset += bytes_per_pixel
                pixels.extend(pixel * count)
            else:
                size = count * bytes_per_pixel
                pixels.extend(raw[offset:offset + size])
                offset += size
    prefix = raw[:prefix_length]
    prefix[2] = 2
    return prefix, pixels[:total], bytes_per_pixel


def tune_tga(path: Path, settings: dict, light_block: bool = False) -> None:
    prefix, pixels, step = decode_tga(path)
    multiplier = settings["emissive"] / 100
    for index in range(0, len(pixels), step):
        if light_block:
            pixels[index + 1] = min(255, round(pixels[index + 1] * multiplier))
            continue
        metal = pixels[index + 2] > 8
        metalness, roughness = material_targets(settings, metal)
        pixels[index] = round(pixels[index] * 0.25 + roughness * 0.75)
        pixels[index + 1] = min(255, round(pixels[index + 1] * multiplier))
        pixels[index + 2] = metalness if metal else 0
    path.write_bytes(bytes(prefix + pixels))


def tune_png_material(path: Path, settings: dict) -> None:
    width, height, color_type, rows = read_png(path)
    channels = {2: 3, 6: 4}.get(color_type)
    if channels is None:
        raise ValueError(f"未対応のMER PNGです: {path.name}")
    multiplier = settings["emissive"] / 100
    for row in rows:
        for index in range(0, len(row), channels):
            metal = row[index] > 8
            metalness, roughness = material_targets(settings, metal)
            row[index] = metalness if metal else 0
            row[index + 1] = min(255, round(row[index + 1] * multiplier))
            row[index + 2] = round(row[index + 2] * 0.25 + roughness * 0.75)
    write_png(path, width, height, color_type, rows)


def tune_water_transparency(pack_path: Path, transparency: int, progress=None) -> int:
    """水のRGB色は維持し、アルファだけを透明度の絶対値へ合わせる。"""
    ratio = max(0.0, min(1.0, transparency / 100))
    # 100%ではアルファ35まで下げ、暗視ポーション級の水面透過にする。
    target_max_alpha = round(255 - 220 * ratio)
    names = (
        "water_still.png",
        "water_flow.png",
        "water_still_grey.png",
        "water_flow_grey.png",
        "water_placeholder.png",
    )
    changed = 0
    root = pack_path / "textures" / "blocks"
    for index, name in enumerate(names):
        path = root / name
        if not path.is_file():
            continue
        width, height, color_type, rows = read_png(path)
        channels = {4: 2, 6: 4}.get(color_type)
        if channels is None:
            continue
        alpha_index = channels - 1
        maximum = max((row[pos] for row in rows for pos in range(alpha_index, len(row), channels)), default=255)
        maximum = max(1, maximum)
        for row in rows:
            for pos in range(alpha_index, len(row), channels):
                row[pos] = max(0, min(255, round(row[pos] * target_max_alpha / maximum)))
        write_png(path, width, height, color_type, rows)
        changed += 1
        if progress:
            progress(80 + index, f"水面の透明度を調整しています… {changed}")
    return changed


def is_matte_protected(color: object) -> bool:
    name = str(color or "").lower()
    return "quartz" in name or "calcite" in name or name in {"grass_side", "grass_top", "grass_carried", "grass_side_carried", "dirt"}


def is_metal_or_gem(color: object) -> bool:
    name = str(color or "").lower()
    if "quartz" in name or "calcite" in name or "command_block" in name:
        return False
    return any(token in name for token in METAL_GEM_TOKENS)


def resolve_material(folder: Path, reference: str) -> Path | None:
    candidate = folder / reference
    if candidate.suffix and candidate.is_file():
        return candidate
    for suffix in (".tga", ".png", ".jpg", ".jpeg"):
        path = folder / f"{reference}{suffix}"
        if path.is_file():
            return path
    return None


def ensure_light_block_assets(pack_path: Path) -> None:
    if not EMBEDDED_PACK.is_file():
        raise FileNotFoundError(f"ライトブロック修正データがありません: {EMBEDDED_PACK}")
    with zipfile.ZipFile(EMBEDDED_PACK) as archive:
        if archive.testzip():
            raise ValueError("内蔵パックが壊れています。ZIPをもう一度ダウンロードしてください。")
        for name in archive.namelist():
            if not re.fullmatch(
                r"(?:local_lighting/local_lighting\.json|textures/items/light_block_(?:[0-9]|1[0-5])(?:\.png|_mers\.tga|\.texture_set\.json))",
                name,
            ):
                continue
            target = pack_path / Path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    remove_deprecated_light_block_shape(pack_path)


def remove_deprecated_light_block_shape(pack_path: Path) -> bool:
    """Remove the old RTX blockshape workaround without touching other mappings."""
    blocks_path = pack_path / "blocks.json"
    if not blocks_path.is_file():
        return False
    data = read_json(blocks_path)
    light_entry = data.get("light_block")
    if not isinstance(light_entry, dict) or "blockshape" not in light_entry:
        return False
    light_entry.pop("blockshape", None)
    if not light_entry:
        data.pop("light_block", None)
    if set(data) <= {"format_version"}:
        blocks_path.unlink()
    else:
        write_json(blocks_path, data)
    return True


def tune_texture_sets(pack_path: Path, settings: dict, progress) -> None:
    roots = [pack_path / "textures" / "blocks", pack_path / "textures" / "items"]
    texture_sets = [path for root in roots if root.is_dir() for path in root.rglob("*.texture_set.json")]
    materials: dict[Path, bool] = {}
    for index, path in enumerate(texture_sets):
        data = read_json(path)
        entry = data.get("minecraft:texture_set", {})
        color = entry.get("color")
        material = entry.get("metalness_emissive_roughness")
        if material is None and "metalness_emissive_roughness_subsurface" in entry:
            material = entry.pop("metalness_emissive_roughness_subsurface")
            entry["metalness_emissive_roughness"] = material
        protected = is_matte_protected(color)
        full_metal = is_metal_or_gem(color) and "ore" not in str(color).lower()
        if isinstance(material, list) and len(material) >= 3:
            metalness, roughness = material_targets(settings, full_metal and not protected)
            material[0] = 0 if protected else metalness
            material[1] = min(255, round(float(material[1] or 0) * settings["emissive"] / 100))
            material[2] = max(210, roughness) if protected else roughness
            entry["metalness_emissive_roughness"] = material
        elif isinstance(material, str):
            resolved = resolve_material(path.parent, material)
            if resolved:
                materials[resolved] = "light_block_" in str(color).lower()
        write_json(path, data)
        if index % 180 == 0:
            progress(38 + round(12 * index / max(1, len(texture_sets))), f"材質設定を確認しています… {index}/{len(texture_sets)}")
    for index, (path, light_block) in enumerate(materials.items()):
        if path.suffix.lower() == ".tga":
            tune_tga(path, settings, light_block)
        elif path.suffix.lower() == ".png":
            tune_png_material(path, settings)
        if index % 80 == 0:
            progress(52 + round(22 * index / max(1, len(materials))), f"光沢・発光・鏡面を調整しています… {index}/{len(materials)}")


def tune_fog_lighting(pack_path: Path, settings: dict) -> None:
    fog_path = pack_path / "fogs" / "overworld_fog.json"
    if fog_path.is_file():
        fog = read_json(fog_path)
        entry = fog.get("minecraft:fog_settings", {})
        ratio = settings["fog"] / 100
        start = max(0.02, 0.90 - 0.75 * ratio)
        end = max(start + 0.05, 1.0 - 0.70 * ratio)
        if isinstance(entry.get("distance", {}).get("air"), dict):
            entry["distance"]["air"].update(fog_start=round(start, 3), fog_end=round(end, 3))
        if isinstance(entry.get("distance", {}).get("weather"), dict):
            entry["distance"]["weather"].update(fog_start=round(max(0.02, start * 0.55), 3), fog_end=round(max(start * 0.55 + 0.05, end * 0.85), 3))
        clarity = max(0.0, min(1.0, settings["water_transparency"] / 100))
        distance = entry.setdefault("distance", {})
        distance["water"] = {
            "fog_start": round(0.05 + 0.90 * clarity, 3),
            "fog_end": round(0.35 + 0.65 * clarity, 3),
            "fog_color": "#3F91B4",
            "render_distance_type": "render",
        }
        air = entry.get("volumetric", {}).get("density", {}).get("air")
        if isinstance(air, dict):
            air["max_density"] = round(0.001 + 0.045 * ratio, 4)
        volumetric = entry.setdefault("volumetric", {})
        density = volumetric.setdefault("density", {})
        remaining = (1.0 - clarity) ** 2
        density["water"] = {
            "max_density": round(0.300 * remaining, 4),
            "uniform": True,
        }
        coefficients = volumetric.setdefault("media_coefficients", {})
        coefficients["water"] = {
            "scattering": [
                round(0.030 * remaining, 4),
                round(0.050 * remaining, 4),
                round(0.070 * remaining, 4),
            ],
            "absorption": [
                round(0.100 * remaining, 4),
                round(0.050 * remaining, 4),
                round(0.030 * remaining, 4),
            ],
        }
        write_json(fog_path, fog)
    lighting_path = pack_path / "lighting" / "global.json"
    if lighting_path.is_file():
        lighting = repair_lighting_settings(read_json(lighting_path))
    else:
        identifier = safe_key(pack_path.name.lower())[:48]
        lighting = {
            "format_version": "1.21.80",
            "minecraft:lighting_settings": {
                "description": {"identifier": f"my_original_rtx_manager:{identifier}_lighting"},
                "ambient": {},
                "sky": {},
                "emissive": {},
            },
        }
        lighting = repair_lighting_settings(lighting)
    entry = lighting.setdefault("minecraft:lighting_settings", {})
    ambient = entry.setdefault("ambient", {})
    sky = entry.setdefault("sky", {})
    emissive = entry.setdefault("emissive", {})
    if settings.get("night_vision"):
        ambient.update(illuminance=5.0, color="#E8F2FF")
        sky["intensity"] = 1.0
    elif settings.get("ambient"):
        ambient.update(illuminance=0.022, color="#D8E4FF")
        sky["intensity"] = 0.42
    else:
        ambient.update(illuminance=0.004, color="#D8E4FF")
        sky["intensity"] = 0.15
    emissive["desaturation"] = 0.0
    write_json(lighting_path, lighting)
    shadow_path = pack_path / "shadows" / "global.json"
    if shadow_path.is_file():
        shadows = read_json(shadow_path)
        entry = shadows.get("minecraft:shadow_settings", {})
        if isinstance(entry, dict):
            entry["shadow_style"] = "soft_shadows"
            entry.pop("texel_size", None)
        write_json(shadow_path, shadows)


def send_to_recycle_bin(path: Path) -> None:
    if os.name != "nt":
        raise OSError("この機能はWindows専用です。")
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p),
        ]
    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 3
    operation.pFrom = str(path) + "\0\0"
    operation.fFlags = 0x0040 | 0x0010 | 0x0004
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError(f"ゴミ箱への移動に失敗しました（Windowsコード: {result}）")


def rounded_polygon(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, radius: float, **kwargs):
    radius = max(2, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=18, **kwargs)


class ModernButton(tk.Canvas):
    PALETTES = {
        "secondary": ("#33393b", "#41494c", "#f3f7f4", "#3a4244"),
        "primary": ("#31875a", "#5fd18d", "#ffffff", "#3ca66d"),
        "launch": ("#286f4a", "#54bf7f", "#ffffff", "#31875a"),
        "danger": ("#392a2d", "#8f454b", "#ffd4d6", "#483034"),
        "quiet": ("#24292b", "#3d4548", "#e8eeeb", "#303638"),
    }

    def __init__(
        self,
        parent,
        text: str,
        command=None,
        variant: str = "secondary",
        height: int = 44,
        width: int = 150,
        align: str = "center",
    ) -> None:
        super().__init__(
            parent,
            height=height,
            width=width,
            bg=parent.cget("bg"),
            highlightthickness=0,
            borderwidth=0,
            cursor="hand2",
        )
        self.button_text = text
        self.command = command
        self.variant = variant
        self.align = align
        self.widget_state = "normal"
        self.hovered = False
        self.bind("<Configure>", lambda _event: self.redraw())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonRelease-1>", self._activate)
        self.after_idle(self.redraw)

    def _enter(self, _event=None) -> None:
        if self.widget_state != "disabled":
            self.hovered = True
            self.redraw()

    def _leave(self, _event=None) -> None:
        self.hovered = False
        self.redraw()

    def _activate(self, _event=None) -> None:
        if self.widget_state != "disabled" and self.command:
            self.command()

    def configure(self, cnf=None, **kwargs):
        values = dict(cnf or {}) if isinstance(cnf, dict) else {}
        values.update(kwargs)
        if "text" in values:
            self.button_text = str(values.pop("text"))
        if "state" in values:
            self.widget_state = str(values.pop("state"))
            super().configure(cursor="arrow" if self.widget_state == "disabled" else "hand2")
        result = super().configure(**values) if values else None
        self.redraw()
        return result

    config = configure

    def redraw(self) -> None:
        if not self.winfo_exists():
            return
        self.delete("all")
        width = max(20, self.winfo_width())
        height = max(20, self.winfo_height())
        fill, border, foreground, hover = self.PALETTES.get(self.variant, self.PALETTES["secondary"])
        if self.hovered and self.widget_state != "disabled":
            fill = hover
        if self.widget_state == "disabled":
            fill, border, foreground = "#252a2b", "#343a3c", "#707978"
        rounded_polygon(self, 1, 1, width - 1, height - 1, 9, fill=border, outline="")
        rounded_polygon(self, 2, 2, width - 2, height - 2, 8, fill=fill, outline="")
        lines = self.button_text.split("\n", 1)
        anchor = "w" if self.align == "left" else "center"
        x = 16 if self.align == "left" else width / 2
        if len(lines) == 1:
            self.create_text(x, height / 2, text=lines[0], anchor=anchor, fill=foreground, font=("Yu Gothic UI", 10, "bold"))
        else:
            self.create_text(x, height / 2 - 7, text=lines[0], anchor=anchor, fill=foreground, font=("Yu Gothic UI", 10, "bold"))
            muted = "#b9c3be" if self.widget_state != "disabled" else "#68706d"
            self.create_text(x, height / 2 + 10, text=lines[1], anchor=anchor, fill=muted, font=("Yu Gothic UI", 8))


class ModernScale(tk.Canvas):
    def __init__(self, parent, variable: tk.DoubleVar, start: float, end: float, step: float, accent: str, accent_dark: str) -> None:
        super().__init__(parent, height=28, bg=parent.cget("bg"), highlightthickness=0, borderwidth=0, cursor="hand2")
        self.variable = variable
        self.start = float(start)
        self.end = float(end)
        self.step = float(step)
        self.accent = accent
        self.accent_dark = accent_dark
        self.variable.trace_add("write", lambda *_: self.redraw())
        self.bind("<Configure>", lambda _event: self.redraw())
        self.bind("<Button-1>", self._set_from_event)
        self.bind("<B1-Motion>", self._set_from_event)
        self.after_idle(self.redraw)

    def _set_from_event(self, event) -> None:
        width = max(1, self.winfo_width() - 24)
        ratio = max(0.0, min(1.0, (event.x - 12) / width))
        raw = self.start + ratio * (self.end - self.start)
        value = round(raw / self.step) * self.step
        self.variable.set(max(self.start, min(self.end, value)))

    def redraw(self) -> None:
        if not self.winfo_exists():
            return
        self.delete("all")
        width = max(32, self.winfo_width())
        x1, x2, y = 12, width - 12, 14
        denominator = max(1e-9, self.end - self.start)
        ratio = max(0.0, min(1.0, (float(self.variable.get()) - self.start) / denominator))
        knob_x = x1 + ratio * (x2 - x1)
        self.create_line(x1, y, x2, y, fill="#737b7d", width=6, capstyle=tk.ROUND)
        if knob_x > x1:
            self.create_line(x1, y, knob_x, y, fill=self.accent, width=6, capstyle=tk.ROUND)
        self.create_oval(knob_x - 10, y - 10, knob_x + 10, y + 10, fill="#21352a", outline="")
        self.create_oval(knob_x - 7, y - 7, knob_x + 7, y + 7, fill="#d9ffe7", outline=self.accent_dark, width=3)


class ToggleSwitch(tk.Canvas):
    def __init__(self, parent, variable: tk.BooleanVar, command=None, accent: str = "#5fd18d", accent_dark: str = "#2f8555") -> None:
        super().__init__(parent, width=50, height=28, bg=parent.cget("bg"), highlightthickness=0, borderwidth=0, cursor="hand2")
        self.variable = variable
        self.command = command
        self.accent = accent
        self.accent_dark = accent_dark
        self.widget_state = "normal"
        self.variable.trace_add("write", lambda *_: self.redraw())
        self.bind("<Button-1>", self._toggle)
        self.bind("<Configure>", lambda _event: self.redraw())
        self.after_idle(self.redraw)

    def _toggle(self, _event=None) -> None:
        if self.widget_state == "disabled":
            return
        self.variable.set(not self.variable.get())
        if self.command:
            self.command()

    def configure(self, cnf=None, **kwargs):
        values = dict(cnf or {}) if isinstance(cnf, dict) else {}
        values.update(kwargs)
        if "state" in values:
            self.widget_state = str(values.pop("state"))
            super().configure(cursor="arrow" if self.widget_state == "disabled" else "hand2")
        result = super().configure(**values) if values else None
        self.redraw()
        return result

    config = configure

    def redraw(self) -> None:
        if not self.winfo_exists():
            return
        self.delete("all")
        enabled = bool(self.variable.get())
        if self.widget_state == "disabled":
            track, border, knob = "#282d2f", "#3b4244", "#707779"
        elif enabled:
            track, border, knob = self.accent_dark, self.accent, "#ffffff"
        else:
            track, border, knob = "#171a1b", "#51595b", "#a0a7a8"
        rounded_polygon(self, 1, 2, 49, 26, 12, fill=border, outline="")
        rounded_polygon(self, 2, 3, 48, 25, 11, fill=track, outline="")
        center = 36 if enabled else 14
        self.create_oval(center - 9, 5, center + 9, 23, fill=knob, outline="")


class ManagerApp:
    BG = "#181b1d"
    PANEL = "#252a2c"
    PANEL2 = "#2d3335"
    BORDER = "#41494c"
    TEXT = "#f3f7f4"
    MUTED = "#aab5b0"
    GREEN = "#5fd18d"
    GREEN_DARK = "#2f8555"
    YELLOW = "#f5bf55"
    RED = "#ff737b"
    BLUE = "#68b8ff"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1680x940")
        self.root.minsize(920, 600)
        self.root.configure(bg=self.BG)
        self.packs: list[PackInfo] = []
        self.selected: PackInfo | None = None
        self.scan_issues: list[tuple[Path, str]] = []
        self.diagnostics: list[str] = []
        self.busy = False
        self.compact = False
        self.settings_compact = False
        self.toolbar_compact = False
        self.action_buttons: list[tk.Widget] = []

        self.preview_var = tk.BooleanVar(value=False)
        self.pack_var = tk.StringVar()
        self.gpu_var = tk.StringVar(value=DEFAULT_GPU)
        self.fog_var = tk.DoubleVar(value=DEFAULTS["fog"])
        self.emissive_var = tk.DoubleVar(value=DEFAULTS["emissive"])
        self.relief_var = tk.DoubleVar(value=DEFAULTS["relief"])
        self.density_var = tk.DoubleVar(value=DEFAULTS["density"])
        self.mirror_var = tk.DoubleVar(value=DEFAULTS["mirror"])
        self.roughness_var = tk.DoubleVar(value=DEFAULTS["roughness"])
        self.water_var = tk.DoubleVar(value=DEFAULTS["water_transparency"])
        self.ambient_var = tk.BooleanVar(value=DEFAULTS["ambient"])
        self.night_vision_var = tk.BooleanVar(value=DEFAULTS["night_vision"])
        self.anti_var = tk.BooleanVar(value=DEFAULTS["anti_flicker"])
        self.status_var = tk.StringVar(value="Minecraftと対象パックを検索しています…")
        self.progress_var = tk.DoubleVar(value=0)
        self.current_name_var = tk.StringVar(value="パックを検索しています")
        self.current_path_var = tk.StringVar(value="")
        self.diag_title_var = tk.StringVar(value="状態を確認しています")
        self.diag_message_var = tk.StringVar(value="Minecraftとリソースパックを調べています。")
        self.diag_action_var = tk.StringVar(value="そのままお待ちください。")
        self.diag_code_var = tk.StringVar(value="CHECKING")
        self.diag_time_var = tk.StringVar(value="--:--:--")

        self.configure_styles()
        self.build_ui()
        self.root.bind("<Configure>", self.on_root_resize, add="+")
        self.root.after(100, self.refresh_packs)
        self.root.after(2500, self.automatic_update_check)

    def configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL, relief="flat")
        style.configure("TLabel", background=self.BG, foreground=self.TEXT, font=("Yu Gothic UI", 10))
        style.configure("Panel.TLabel", background=self.PANEL, foreground=self.TEXT)
        style.configure("Muted.TLabel", background=self.PANEL, foreground=self.MUTED, font=("Yu Gothic UI", 9))
        style.configure("Title.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Yu Gothic UI", 15, "bold"))
        style.configure("TButton", background=self.PANEL2, foreground=self.TEXT, bordercolor=self.BORDER, padding=(12, 9), font=("Yu Gothic UI", 10, "bold"))
        style.map("TButton", background=[("active", "#394143"), ("disabled", "#25292a")], foreground=[("disabled", "#707978")])
        style.configure("TCombobox", fieldbackground="#1e2223", background=self.PANEL2, foreground=self.TEXT, arrowcolor=self.TEXT, bordercolor=self.BORDER, padding=8, font=("Yu Gothic UI", 10))
        style.map("TCombobox", fieldbackground=[("readonly", "#1e2223")], foreground=[("readonly", self.TEXT)])
        style.configure("Vertical.TScrollbar", background="#343b3d", troughcolor=self.BG, bordercolor=self.BG, arrowcolor=self.MUTED)
        style.configure("Horizontal.TProgressbar", troughcolor="#303638", background=self.GREEN, bordercolor="#303638", lightcolor=self.GREEN, darkcolor=self.GREEN)

    def build_ui(self) -> None:
        self.header = tk.Frame(self.root, bg="#101719", height=68, highlightthickness=0)
        self.header.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.header.grid_propagate(False)

        brand = tk.Frame(self.header, bg="#151817", width=38, height=38, highlightbackground="#46504b", highlightthickness=1)
        brand.pack(side="left", padx=(24, 12))
        brand.pack_propagate(False)
        blocks = tk.Frame(brand, bg="#151817")
        blocks.pack(expand=True)
        for row in range(2):
            for column in range(2):
                color = self.GREEN if row == column else "#376a4c"
                tk.Frame(blocks, width=10, height=10, bg=color).grid(row=row, column=column, padx=2, pady=2)

        self.header_titles = tk.Frame(self.header, bg="#101719")
        self.header_titles.pack(side="left", pady=10)
        tk.Label(self.header_titles, text=APP_NAME, bg="#101719", fg=self.TEXT, font=("Yu Gothic UI", 19, "bold")).pack(anchor="w")
        self.subtitle_label = tk.Label(
            self.header_titles,
            text="Bedrock RTX・Vibrant Visuals パック管理（Python版）",
            bg="#101719",
            fg=self.MUTED,
            font=("Yu Gothic UI", 9),
        )
        self.subtitle_label.pack(anchor="w")

        self.header_actions = tk.Frame(self.header, bg="#101719")
        self.header_actions.pack(side="right", padx=22)
        self.gpu_selector_shell = tk.Frame(
            self.header_actions,
            bg="#253a30",
            padx=8,
            pady=4,
            highlightbackground="#3f7657",
            highlightthickness=1,
        )
        tk.Label(
            self.gpu_selector_shell,
            text="GPU推奨設定",
            bg="#253a30",
            fg="#9fd9b7",
            font=("Yu Gothic UI", 8, "bold"),
        ).pack(side="left", padx=(2, 7))
        self.gpu_combo = ttk.Combobox(
            self.gpu_selector_shell,
            textvariable=self.gpu_var,
            values=list(GPU_PROFILES),
            state="readonly",
            width=10,
        )
        self.gpu_combo.pack(side="left")
        self.gpu_combo.bind("<<ComboboxSelected>>", self.on_gpu_selected)
        self.gpu_selector_shell.pack(side="left", padx=(0, 8))
        self.recommend_button = ModernButton(self.header_actions, text="推奨設定を適用", command=self.reset_values, variant="primary", width=145, height=38)
        self.recommend_button.pack(side="left", padx=(0, 8))
        self.help_button = ModernButton(self.header_actions, text="?", command=self.show_help, variant="quiet", width=40, height=38)
        self.help_button.pack(side="left")

        self.toolbar = tk.Frame(self.root, bg="#202426", padx=18, pady=10, highlightbackground="#343a3d", highlightthickness=1)
        self.toolbar.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.select_button = ModernButton(self.toolbar, text="⌕  パックを手動選択", command=self.select_folder, variant="secondary", height=44)

        self.preview_shell = tk.Frame(self.toolbar, bg="#2b3032", highlightbackground="#3a4143", highlightthickness=1, padx=13, pady=8)
        self.preview_button = ToggleSwitch(self.preview_shell, self.preview_var, command=self.refresh_packs, accent=self.GREEN, accent_dark=self.GREEN_DARK)
        self.preview_button.pack(side="left")
        tk.Label(self.preview_shell, text="Minecraft Preview", bg="#2b3032", fg=self.TEXT, font=("Yu Gothic UI", 10, "bold")).pack(side="left", padx=(9, 2))

        self.pack_shell = tk.Frame(self.toolbar, bg="#2b3032", highlightbackground="#3a4143", highlightthickness=1, padx=12, pady=6)
        tk.Label(self.pack_shell, text="使用するパック", bg="#2b3032", fg=self.TEXT, font=("Yu Gothic UI", 9, "bold")).pack(side="left", padx=(0, 9))
        self.pack_combo = ttk.Combobox(self.pack_shell, textvariable=self.pack_var, state="readonly")
        self.pack_combo.pack(side="left", fill="x", expand=True)
        self.pack_combo.bind("<<ComboboxSelected>>", self.on_pack_selected)
        self.refresh_button = ModernButton(self.pack_shell, text="↻", command=self.refresh_packs, variant="quiet", width=42, height=36)
        self.refresh_button.pack(side="left", padx=(8, 0))
        self.layout_toolbar(False)

        self.canvas = tk.Canvas(self.root, bg=self.BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.root, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.grid(row=2, column=0, sticky="nsew")
        scrollbar.grid(row=2, column=1, sticky="ns")
        self.content = tk.Frame(self.canvas, bg=self.BG, padx=20, pady=18)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        self.root.bind_all("<MouseWheel>", self.on_mousewheel, add="+")

        self.left = tk.Frame(self.content, bg=self.PANEL, padx=16, pady=16, highlightbackground=self.BORDER, highlightthickness=1)
        self.right = tk.Frame(self.content, bg=self.BG)
        self.left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self.right.grid(row=0, column=1, sticky="nsew")
        self.content.columnconfigure(0, minsize=300)
        self.content.columnconfigure(1, weight=1)
        self.build_left()
        self.build_right()

        status = tk.Frame(self.root, bg="#101719", padx=20, pady=7, highlightbackground="#343a3d", highlightthickness=1)
        status.grid(row=3, column=0, columnspan=2, sticky="ew")
        ttk.Progressbar(status, variable=self.progress_var, maximum=100).pack(fill="x")
        tk.Label(status, textvariable=self.status_var, bg="#202426", fg=self.MUTED, font=("Yu Gothic UI", 9)).pack(pady=(5, 0))
        self.status_detail = tk.Label(status, text=f"● ！34準備中　　✓ 水透明度100%　　✓ 洞窟環境光MAX　　App {APP_VERSION_NUMBER}", bg="#101719", fg=self.MUTED, font=("Yu Gothic UI", 8))
        self.status_detail.pack(anchor="w", pady=(4, 0))

        self.root.rowconfigure(2, weight=1)
        self.root.columnconfigure(0, weight=1)

    def build_left(self) -> None:
        tk.Label(self.left, text="現在のパック情報", bg=self.PANEL, fg=self.GREEN, font=("Yu Gothic UI", 10, "bold")).pack(anchor="w")
        tk.Label(self.left, textvariable=self.current_name_var, bg=self.PANEL, fg=self.TEXT, font=("Yu Gothic UI", 16, "bold"), wraplength=310, justify="left").pack(anchor="w", pady=(6, 4))
        tk.Label(self.left, textvariable=self.current_path_var, bg=self.PANEL, fg=self.MUTED, font=("Yu Gothic UI", 8), wraplength=270, justify="left").pack(anchor="w")

        protection = tk.Frame(self.left, bg="#253a30", highlightbackground="#3e6d51", highlightthickness=1, padx=13, pady=11)
        protection.pack(fill="x", pady=13)
        tk.Label(protection, text="✓  今回の保護設定", bg="#253a30", fg="#d8ffe7", font=("Yu Gothic UI", 10, "bold")).pack(anchor="w", pady=(0, 6))
        tk.Label(
            protection,
            text="• 公式16pxのRGB色を維持\n• 水の透明度100%\n• 水中フォグ・散乱・吸収0\n• 洞窟の最低環境光を上限5.0\n• 空の間接光を上限1.0\n• ライトは手持ち時だけ標準表示\n• GitHubから安全確認後に自動更新\n• 変更前に自動バックアップ",
            bg="#253a30",
            fg="#dce7e1",
            font=("Yu Gothic UI", 9),
            justify="left",
        ).pack(anchor="w")

        self.diag_frame = tk.Frame(self.left, bg="#202628", highlightbackground="#46606a", highlightthickness=1, padx=12, pady=11)
        self.diag_frame.pack(fill="x", pady=(0, 10))
        tk.Label(self.diag_frame, text="トラブル診断", bg="#202628", fg=self.BLUE, font=("Yu Gothic UI", 9, "bold")).pack(anchor="w", pady=(0, 5))
        self.diag_title = tk.Label(self.diag_frame, textvariable=self.diag_title_var, bg="#202628", fg=self.BLUE, font=("Yu Gothic UI", 11, "bold"), anchor="w")
        self.diag_title.pack(fill="x")
        tk.Label(self.diag_frame, textvariable=self.diag_message_var, bg="#202628", fg=self.TEXT, font=("Yu Gothic UI", 9), wraplength=295, justify="left").pack(anchor="w", pady=(6, 5))
        solution = tk.Frame(self.diag_frame, bg="#1a1f20", padx=9, pady=7)
        solution.pack(fill="x")
        tk.Label(solution, text="対処方法", bg="#1a1f20", fg=self.GREEN, font=("Yu Gothic UI", 8, "bold")).pack(anchor="w")
        tk.Label(solution, textvariable=self.diag_action_var, bg="#1a1f20", fg=self.MUTED, font=("Yu Gothic UI", 8), wraplength=275, justify="left").pack(anchor="w")
        meta = tk.Frame(self.diag_frame, bg="#202628")
        meta.pack(fill="x", pady=(6, 4))
        tk.Label(meta, textvariable=self.diag_code_var, bg="#202628", fg=self.BLUE, font=("Consolas", 8)).pack(side="left")
        tk.Label(meta, textvariable=self.diag_time_var, bg="#202628", fg=self.MUTED, font=("Yu Gothic UI", 8)).pack(side="right")
        ModernButton(self.diag_frame, text="↻  今すぐ診断", command=self.refresh_packs, variant="quiet", height=36).pack(fill="x", pady=(3, 6))
        self.history = tk.Listbox(self.diag_frame, height=4, bg="#1d2122", fg=self.MUTED, selectbackground=self.GREEN_DARK, borderwidth=0, font=("Yu Gothic UI", 8))
        self.history.pack(fill="x")
        warning = tk.Frame(self.left, bg="#343126", highlightbackground="#665b3c", highlightthickness=1, padx=11, pady=9)
        warning.pack(fill="x", pady=(1, 10))
        tk.Label(warning, text="重要", bg="#343126", fg=self.YELLOW, font=("Yu Gothic UI", 9, "bold")).pack(anchor="w")
        tk.Label(warning, text="適用するときはMinecraftを終了してください。反映にはワールドへの入り直しが必要です。", bg="#343126", fg="#ddd6c4", font=("Yu Gothic UI", 8), wraplength=290, justify="left").pack(anchor="w", pady=(3, 0))
        tk.Label(self.left, text=f"App Version  {APP_VERSION}", bg=self.PANEL, fg=self.MUTED, font=("Yu Gothic UI", 8)).pack(anchor="e", pady=(2, 0))

    def build_right(self) -> None:
        quick = tk.Frame(self.right, bg=self.BG)
        quick.pack(fill="x", pady=(0, 10))
        tk.Label(quick, text="ビジュアル設定", bg=self.BG, fg=self.TEXT, font=("Yu Gothic UI", 16, "bold")).pack(side="left")
        tk.Label(quick, text="リアルタイムでMinecraftの見え方を調整します", bg=self.BG, fg=self.MUTED, font=("Yu Gothic UI", 9)).pack(side="left", padx=10, pady=(5, 0))
        ModernButton(quick, text="↶  推奨値に戻す", command=self.reset_values, variant="secondary", width=160, height=40).pack(side="right")

        self.settings_shell = tk.Frame(self.right, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1, padx=18, pady=8)
        self.settings_shell.pack(fill="x")
        self.left_settings = tk.Frame(self.settings_shell, bg=self.PANEL, padx=10, pady=5)
        self.right_settings = tk.Frame(self.settings_shell, bg=self.PANEL, padx=10, pady=5, highlightbackground="#3a4143", highlightthickness=0)
        self.material_settings = self.right_settings
        self.stability_settings = tk.Frame(self.settings_shell, bg=self.PANEL, padx=10, pady=5)
        self.left_settings.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.right_settings.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        self.stability_settings.grid(row=0, column=2, sticky="nsew", padx=(12, 0))
        self.settings_shell.columnconfigure((0, 1, 2), weight=1, uniform="settings")
        tk.Label(self.left_settings, text="☀  光・視界", bg=self.PANEL, fg=self.YELLOW, font=("Yu Gothic UI", 11, "bold")).pack(anchor="w", pady=(4, 3))
        tk.Label(self.right_settings, text="◇  質感（マテリアル）", bg=self.PANEL, fg=self.GREEN, font=("Yu Gothic UI", 11, "bold")).pack(anchor="w", pady=(4, 3))
        tk.Label(self.stability_settings, text="♢  安定性・最適化", bg=self.PANEL, fg=self.GREEN, font=("Yu Gothic UI", 11, "bold")).pack(anchor="w", pady=(4, 3))
        self.add_slider(self.left_settings, "霧の濃さ", self.fog_var, 0, 100, "右ほど独自の霧が濃くなります", "")
        self.add_slider(self.left_settings, "発光の明るさ", self.emissive_var, 0, 200, "光源とライトブロックの発光を調整", "%")
        self.add_slider(self.right_settings, "金属・宝石の鏡面反射", self.mirror_var, 0, 100, "色を残したまま反射を変更", "%")
        self.add_slider(self.right_settings, "表面の粗さ", self.roughness_var, 0, 100, "右ほど落ち着いた反射", "")
        self.add_slider(self.right_settings, "凹凸の強さ（MER）", self.relief_var, 1, 5, "公式色は変えず立体感だけ調整", " / 5")
        self.add_slider(self.right_settings, "凹凸の密度", self.density_var, 1, 5, "凹凸が現れる場所の多さ", " / 5")
        self.add_slider(self.left_settings, "水の透明度", self.water_var, 0, 100, "100%で水中フォグ・散乱・吸収を0にします", "%")
        self.add_toggle(self.stability_settings, "環境光を強くする", "全体の環境光を強化します", self.ambient_var)
        self.add_toggle(self.stability_settings, "洞窟・暗所を最大環境光にする", "ONで公式上限5.0 lux・空の間接光1.0", self.night_vision_var)
        self.add_toggle(self.stability_settings, "ちらつき防止（アンチフリッカー）", "遠景の細かな凹凸のちらつきを抑えます", self.anti_var)

        apply_row = tk.Frame(self.right, bg=self.BG)
        apply_row.pack(fill="x", pady=12)
        apply_row.columnconfigure(0, weight=1)
        self.apply_button = ModernButton(apply_row, text="⚙  設定を適用", variant="primary", command=self.apply_tuning, height=52)
        self.apply_button.grid(row=0, column=0, sticky="ew")
        self.export_button = ModernButton(apply_row, text="⇩  設定を書き出す", command=self.export_pack, width=165, height=52)
        self.export_button.grid(row=0, column=1, padx=8)
        self.delete_button = ModernButton(apply_row, text="♲  削除", variant="danger", command=self.delete_pack, width=92, height=52)
        self.delete_button.grid(row=0, column=2)

        self.actions = tk.Frame(self.right, bg=self.BG)
        self.actions.pack(fill="x")
        self.install_button = ModernButton(self.actions, text="▣  最新版をインストール\n現在：！34 / 水・洞窟MAX", command=self.install_pack, align="left", height=62)
        self.restore_button = ModernButton(self.actions, text="↶  バックアップから復元\n調整前の状態へ戻す", command=self.restore_pack, align="left", height=62)
        self.folder_button = ModernButton(self.actions, text="▤  パックフォルダーを開く\n保存場所を確認", command=self.open_pack_folder, align="left", height=62)
        self.launch_button = ModernButton(self.actions, text="▶  Minecraft RTXを起動\n調整後にワールドへ入る", variant="launch", command=self.launch_minecraft, align="left", height=62)
        for index, button in enumerate((self.install_button, self.restore_button, self.folder_button, self.launch_button)):
            button.grid(row=index // 2, column=index % 2, sticky="ew", padx=(0 if index % 2 == 0 else 5, 5 if index % 2 == 0 else 0), pady=5)
        self.update_button = ModernButton(self.actions, text="⇩  最新版を確認・更新\nGitHubから安全確認後に自動更新", variant="secondary", command=self.check_for_updates, height=62)
        self.update_button.grid(row=2, column=0, columnspan=2, sticky="ew", pady=5)
        self.actions.columnconfigure((0, 1), weight=1)
        self.action_buttons = [self.select_button, self.refresh_button, self.apply_button, self.export_button, self.delete_button, self.install_button, self.restore_button, self.folder_button, self.launch_button, self.update_button]
        self.update_button_states()

    def add_slider(self, parent, title: str, variable: tk.DoubleVar, start: int, end: int, note: str, suffix: str) -> None:
        row = tk.Frame(parent, bg=self.PANEL, pady=6)
        row.pack(fill="x")
        value_var = tk.StringVar()
        def update(*_):
            value = int(round(variable.get()))
            value_var.set(f"{value}{suffix}")
        variable.trace_add("write", update)
        update()
        top = tk.Frame(row, bg=self.PANEL)
        top.pack(fill="x")
        tk.Label(top, text=title, bg=self.PANEL, fg=self.TEXT, font=("Yu Gothic UI", 10, "bold")).pack(side="left")
        tk.Label(top, textvariable=value_var, bg="#1d2021", fg="#e9f4ee", font=("Yu Gothic UI", 9, "bold"), padx=9, pady=3, highlightbackground="#444b4e", highlightthickness=1).pack(side="right")
        step = 1 if end <= 5 else 10 if end == 200 else 5
        ModernScale(row, variable, start, end, step, self.GREEN, self.GREEN_DARK).pack(fill="x", pady=(3, 0))
        tk.Label(row, text=note, bg=self.PANEL, fg=self.MUTED, font=("Yu Gothic UI", 8)).pack(anchor="w")
        tk.Frame(row, bg="#383e40", height=1).pack(fill="x", pady=(8, 0))

    def add_toggle(self, parent, title: str, note: str, variable: tk.BooleanVar) -> None:
        row = tk.Frame(parent, bg=self.PANEL, pady=10)
        row.pack(fill="x")
        labels = tk.Frame(row, bg=self.PANEL)
        labels.pack(side="left", fill="x", expand=True)
        value_var = tk.StringVar()
        variable.trace_add("write", lambda *_: value_var.set("ON" if variable.get() else "OFF"))
        value_var.set("ON" if variable.get() else "OFF")
        title_row = tk.Frame(labels, bg=self.PANEL)
        title_row.pack(fill="x")
        tk.Label(title_row, text=title, bg=self.PANEL, fg=self.TEXT, font=("Yu Gothic UI", 10, "bold")).pack(side="left")
        tk.Label(title_row, textvariable=value_var, bg="#1d2021", fg="#e9f4ee", font=("Yu Gothic UI", 8, "bold"), padx=7, pady=2).pack(side="left", padx=9)
        tk.Label(labels, text=note, bg=self.PANEL, fg=self.MUTED, font=("Yu Gothic UI", 8)).pack(anchor="w", pady=(4, 0))
        ToggleSwitch(row, variable, accent=self.GREEN, accent_dark=self.GREEN_DARK).pack(side="right", padx=(10, 0))
        tk.Frame(parent, bg="#383e40", height=1).pack(fill="x")

    def show_help(self) -> None:
        messagebox.showinfo(
            "使い方",
            "1. 上部で対象パックを選びます。\n"
            "2. 右上でGPUを選ぶと、そのGPU向け推奨値に変わります。\n"
            "3. 水100%・洞窟最大環境光が初期値です。\n"
            "4. 「設定を適用」を押します。\n"
            "5. Minecraft RTXを起動してワールドへ入り直します。\n\n"
            "GPUを選んだだけではMinecraftのファイルは変更しません。\n"
            "画面が小さいときは縦に並び替わり、マウスホイールで全項目を確認できます。",
        )

    def on_mousewheel(self, event) -> None:
        amount = int(-event.delta / 120) if event.delta else 0
        if amount:
            self.canvas.yview_scroll(amount, "units")

    def layout_toolbar(self, compact: bool) -> None:
        for widget in (self.select_button, self.preview_shell, self.pack_shell):
            widget.grid_forget()
        self.toolbar.columnconfigure(0, weight=0, minsize=0)
        self.toolbar.columnconfigure(1, weight=0, minsize=0)
        self.toolbar.columnconfigure(2, weight=0, minsize=0)
        if compact:
            self.select_button.grid(row=0, column=0, sticky="ew", padx=(0, 5), pady=(0, 7))
            self.preview_shell.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=(0, 7))
            self.pack_shell.grid(row=1, column=0, columnspan=2, sticky="ew")
            self.toolbar.columnconfigure((0, 1), weight=1)
        else:
            self.select_button.grid(row=0, column=0, sticky="ew", padx=(0, 7))
            self.preview_shell.grid(row=0, column=1, sticky="ew", padx=7)
            self.pack_shell.grid(row=0, column=2, sticky="ew", padx=(7, 0))
            self.toolbar.columnconfigure(0, weight=0, minsize=250)
            self.toolbar.columnconfigure(1, weight=0, minsize=215)
            self.toolbar.columnconfigure(2, weight=1, minsize=260)

    def on_root_resize(self, event) -> None:
        if event.widget is not self.root:
            return
        compact = event.width < 820
        if compact != self.toolbar_compact:
            self.toolbar_compact = compact
            self.layout_toolbar(compact)
        if event.width < 720:
            if self.subtitle_label.winfo_manager():
                self.subtitle_label.pack_forget()
            if self.gpu_selector_shell.winfo_manager():
                self.gpu_selector_shell.pack_forget()
            if self.recommend_button.winfo_manager():
                self.recommend_button.pack_forget()
        else:
            if not self.subtitle_label.winfo_manager():
                self.subtitle_label.pack(anchor="w")
            if not self.gpu_selector_shell.winfo_manager():
                self.gpu_selector_shell.pack(side="left", padx=(0, 8), before=self.help_button)
            if not self.recommend_button.winfo_manager():
                self.recommend_button.pack(side="left", padx=(0, 8), before=self.help_button)

    def layout_settings(self, compact: bool) -> None:
        self.left_settings.grid_forget()
        self.right_settings.grid_forget()
        self.stability_settings.grid_forget()
        if compact:
            self.left_settings.grid(row=0, column=0, sticky="nsew")
            self.right_settings.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
            self.stability_settings.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
            self.settings_shell.columnconfigure(0, weight=1)
            self.settings_shell.columnconfigure(1, weight=0)
            self.settings_shell.columnconfigure(2, weight=0)
            for index, button in enumerate((self.install_button, self.restore_button, self.folder_button, self.launch_button)):
                button.grid_configure(row=index, column=0, columnspan=2, padx=0, pady=4)
            self.update_button.grid_configure(row=4, column=0, columnspan=2, pady=5)
        else:
            self.left_settings.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
            self.right_settings.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
            self.stability_settings.grid(row=0, column=2, sticky="nsew", padx=(12, 0))
            self.settings_shell.columnconfigure((0, 1, 2), weight=1, uniform="settings")
            for index, button in enumerate((self.install_button, self.restore_button, self.folder_button, self.launch_button)):
                button.grid_configure(
                    row=index // 2,
                    column=index % 2,
                    columnspan=1,
                    padx=(0 if index % 2 == 0 else 5, 5 if index % 2 == 0 else 0),
                    pady=5,
                )
            self.update_button.grid_configure(row=2, column=0, columnspan=2, pady=5)

    def on_canvas_resize(self, event) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=max(1, event.width))
        compact = event.width < 1180
        if compact != self.compact:
            self.compact = compact
            if compact:
                self.left.grid_configure(row=0, column=0, columnspan=2, padx=0, pady=(0, 14))
                self.right.grid_configure(row=1, column=0, columnspan=2)
                self.content.columnconfigure(0, weight=1, minsize=0)
                self.content.columnconfigure(1, weight=0, minsize=0)
            else:
                self.left.grid_configure(row=0, column=0, columnspan=1, padx=(0, 18), pady=0)
                self.right.grid_configure(row=0, column=1, columnspan=1)
                self.content.columnconfigure(0, weight=0, minsize=300)
                self.content.columnconfigure(1, weight=1)
        settings_compact = event.width < 1280
        if settings_compact != self.settings_compact:
            self.settings_compact = settings_compact
            self.layout_settings(settings_compact)

    def settings(self) -> dict:
        return {
            "fog": int(self.fog_var.get()), "emissive": int(self.emissive_var.get()),
            "relief": int(self.relief_var.get()), "density": int(self.density_var.get()),
            "mirror": int(self.mirror_var.get()), "roughness": int(self.roughness_var.get()),
            "water_transparency": int(self.water_var.get()),
            "ambient": bool(self.ambient_var.get()),
            "night_vision": bool(self.night_vision_var.get()),
            "anti_flicker": bool(self.anti_var.get()),
            "gpu_profile": self.gpu_var.get(),
        }

    def set_status(self, progress: float, message: str) -> None:
        self.progress_var.set(max(0, min(100, progress)))
        self.status_var.set(message)

    def thread_status(self, progress: float, message: str) -> None:
        self.root.after(0, lambda: self.set_status(progress, message))

    def report(self, level: str, title: str, message: str, action: str, code: str) -> None:
        colors = {"success": self.GREEN, "info": self.BLUE, "warning": self.YELLOW, "error": self.RED}
        self.diag_title.configure(fg=colors.get(level, self.BLUE))
        self.diag_title_var.set(title)
        self.diag_message_var.set(message)
        self.diag_action_var.set(action)
        self.diag_code_var.set(code)
        now = time.strftime("%H:%M:%S")
        self.diag_time_var.set(now)
        row = f"{now}  {code}  {title}"
        if not self.diagnostics or self.diagnostics[0] != row:
            self.diagnostics.insert(0, row)
            self.diagnostics = self.diagnostics[:20]
        self.history.delete(0, tk.END)
        for item in self.diagnostics[:8]:
            self.history.insert(tk.END, item)

    def classify_error(self, context: str, error: Exception) -> tuple[str, str, str, str]:
        raw = str(error) or error.__class__.__name__
        lowered = raw.lower()
        operation = {
            "install": "最新版のインストール", "apply": "設定の適用", "restore": "復元",
            "export": "書き出し", "delete": "削除", "scan": "パック検索",
            "folder": "フォルダー表示", "launch": "Minecraftの起動", "update": "自動更新",
        }.get(context, "処理")
        if "backup" in lowered or "バックアップ" in raw:
            return "BACKUP_NOT_FOUND", "バックアップがありません", f"{operation}を続けられませんでした。", "先に一度「選択パックに調整を適用」を押してください。"
        if isinstance(error, PermissionError) or "access is denied" in lowered or "permission" in lowered:
            return "ACCESS_DENIED", "ファイルを変更できません", f"{operation}中にWindowsから拒否されました。", "Minecraftを完全に終了して10秒ほど待ち、もう一度実行してください。"
        if isinstance(error, FileNotFoundError):
            return "FILE_NOT_FOUND", "必要なファイルがありません", raw, "Minecraftを一度起動して終了し、再診断してください。パックの場合は最新版を入れ直してください。"
        if "space" in lowered or "enospc" in lowered or "空き容量" in raw:
            return "DISK_FULL", "空き容量が不足しています", f"{operation}に必要なファイルを保存できませんでした。", "Windowsの空き容量を増やしてからやり直してください。"
        return f"{context.upper()}_FAILED", f"{operation}でエラーが発生しました", raw[:180], "もう一度試してください。直らない場合は、この診断欄のスクリーンショットを送ってください。"

    def set_busy(self, value: bool) -> None:
        self.busy = value
        self.update_button_states()

    def update_button_states(self) -> None:
        selected_state = "disabled" if self.busy or not self.selected else "normal"
        for button in (getattr(self, "apply_button", None), getattr(self, "export_button", None), getattr(self, "delete_button", None), getattr(self, "restore_button", None), getattr(self, "folder_button", None)):
            if button:
                button.configure(state=selected_state)
        for button in (getattr(self, "select_button", None), getattr(self, "refresh_button", None), getattr(self, "install_button", None), getattr(self, "launch_button", None), getattr(self, "update_button", None)):
            if button:
                button.configure(state="disabled" if self.busy else "normal")
        self.pack_combo.configure(state="disabled" if self.busy else "readonly")
        if hasattr(self, "gpu_combo"):
            self.gpu_combo.configure(state="disabled" if self.busy else "readonly")
        if hasattr(self, "preview_button"):
            self.preview_button.configure(state="disabled" if self.busy else "normal")

    def start_task(self, context: str, start_message: str, worker, success) -> None:
        if self.busy:
            return
        self.set_busy(True)
        self.set_status(3, start_message)
        self.report("info", start_message, "処理を実行しています。", "完了するまでMinecraftを起動しないでください。", f"{context.upper()}ING")
        def run():
            try:
                result = worker()
            except Exception as error:
                self.root.after(0, lambda e=error: self.finish_error(context, e))
            else:
                self.root.after(0, lambda r=result: self.finish_success(success, r))
        threading.Thread(target=run, daemon=True).start()

    def finish_error(self, context: str, error: Exception) -> None:
        self.set_busy(False)
        code, title, message, action = self.classify_error(context, error)
        self.report("error", title, message, action, code)
        self.set_status(0, title)
        messagebox.showerror(title, f"{message}\n\n対処方法:\n{action}")

    def finish_success(self, success, result) -> None:
        self.set_busy(False)
        success(result)

    def refresh_packs(self) -> None:
        if self.busy:
            return
        self.set_status(8, "MinecraftとRTX対応パックを検索しています…")
        self.scan_issues = []
        preview = self.preview_var.get()
        candidates = [item for item in minecraft_candidates() if item[1] == preview]
        packs: list[PackInfo] = []
        existing = []
        for label, _, root in candidates:
            if root.is_dir():
                existing.append(root)
            packs += scan_pack_directory(root / "resource_packs", label, "resource_packs", self.scan_issues)
            packs += scan_pack_directory(root / "development_resource_packs", label, "development_resource_packs", self.scan_issues)
        packs.sort(key=lambda pack: (0 if "！34" in pack.name else 1, pack.name))
        for pack in packs:
            if "！34" in pack.name:
                try:
                    repair_pack_lighting(pack.path)
                except Exception as error:
                    self.scan_issues.append((pack.path / "lighting" / "global.json", str(error)))
        self.packs = packs
        self.pack_combo["values"] = [pack.label for pack in packs]
        if packs:
            previous = self.selected.path if self.selected else None
            index = next((i for i, pack in enumerate(packs) if pack.path == previous), 0)
            self.pack_combo.current(index)
            self.select_pack(packs[index])
            self.set_status(0, f"{len(packs)}個のPBRパックを検出しました。")
        else:
            self.pack_var.set("")
            self.select_pack(None)
            self.set_status(0, "PBRパックが見つかりません。先に最新版をインストールしてください。")
        self.run_diagnosis(existing)

    def run_diagnosis(self, existing: list[Path] | None = None) -> None:
        if existing is None:
            existing = [root for _, preview, root in minecraft_candidates() if preview == self.preview_var.get() and root.is_dir()]
        edition = "Minecraft Preview" if self.preview_var.get() else "Minecraft通常版"
        if not existing:
            self.report("warning", f"{edition}の保存場所が見つかりません", "保存フォルダーがまだ作られていない可能性があります。", f"{edition}を一度起動して終了し、今すぐ診断を押してください。", "MINECRAFT_PATH_NOT_FOUND")
            return
        if self.scan_issues:
            self.report("warning", "一部のパックを読み込めません", f"{len(self.scan_issues)}個の設定ファイルに問題があります。", f"問題の場所: {self.scan_issues[0][0]}", "PACK_SCAN_WARNING")
            return
        groups: dict[str, list[PackInfo]] = {}
        for pack in self.packs:
            groups.setdefault(pack.uuid.lower(), []).append(pack)
        duplicate = next((group for group in groups.values() if len(group) > 1), None)
        if duplicate:
            self.report("warning", "重複したパックを検出しました", " / ".join(pack.folder for pack in duplicate), "古い方を赤い削除ボタンでゴミ箱へ移動し、1つだけ残してください。", "DUPLICATE_PACK_UUID")
            return
        if not self.packs:
            self.report("warning", "RTX対応パックがありません", "Minecraftの保存場所は正常です。", "「最新版をインストール」を押してください。", "PBR_PACK_NOT_FOUND")
            return
        if self.selected:
            light_set = self.selected.path / "textures" / "items" / "light_block_15.texture_set.json"
            blocks_path = self.selected.path / "blocks.json"
            local_light_path = self.selected.path / "local_lighting" / "local_lighting.json"
            try:
                entry = read_json(light_set).get("minecraft:texture_set", {})
                local_entry = read_json(local_light_path).get("minecraft:local_light_settings", {}).get("minecraft:light_block", {})
                assets_ready = (
                    "metalness_emissive_roughness" in entry
                    and local_entry.get("light_type") == "static_light"
                )
            except Exception:
                assets_ready = False
            try:
                broken_shape = read_json(blocks_path).get("light_block", {}).get("blockshape") if blocks_path.is_file() else None
            except Exception:
                broken_shape = None
            if broken_shape:
                self.report("error", "！32の破損形状が残っています", f"light_blockへ非対応のblockshape「{broken_shape}」が指定されています。", "Minecraftを終了し、「選択パックに調整を適用」を押すと形状指定を撤去します。", "LIGHT_BLOCK_SHAPE_BROKEN")
                return
            if not assets_ready:
                self.report("warning", "ライト表示用データが不足しています", "手持ち時の標準アイコンまたはVibrant Visuals照明設定を確認できません。", "「選択パックに調整を適用」を押すと、ライト0～15の表示データを追加します。", "LIGHT_BLOCK_ASSETS_MISSING")
                return
            fog_path = self.selected.path / "fogs" / "overworld_fog.json"
            lighting_path = self.selected.path / "lighting" / "global.json"
            try:
                fog_entry = read_json(fog_path).get("minecraft:fog_settings", {})
                water_distance = fog_entry.get("distance", {}).get("water")
                water_density = fog_entry.get("volumetric", {}).get("density", {}).get("water")
                water_media = fog_entry.get("volumetric", {}).get("media_coefficients", {}).get("water", {})
                scattering = [float(value) for value in water_media.get("scattering", [])]
                absorption = [float(value) for value in water_media.get("absorption", [])]
                water_fixed = (
                    isinstance(water_distance, dict)
                    and float(water_distance.get("fog_start", 0)) >= 0.95
                    and float(water_distance.get("fog_end", 0)) >= 1.0
                    and isinstance(water_density, dict)
                    and float(water_density.get("max_density", 1)) == 0.0
                    and len(scattering) == 3
                    and all(value == 0.0 for value in scattering)
                    and len(absorption) == 3
                    and all(value == 0.0 for value in absorption)
                )
            except Exception:
                water_fixed = False
            if not water_fixed:
                self.report("warning", "水の透明度が最大ではありません", "水中フォグ・散乱・吸収のいずれかが0になっていません。", "水の透明度を100%にして「設定を適用」を押してください。", "WATER_CLARITY_NOT_MAXIMUM")
                return
            try:
                lighting_entry = read_json(lighting_path).get("minecraft:lighting_settings", {})
                ambient = lighting_entry.get("ambient", {})
                sky = lighting_entry.get("sky", {})
                cave_maximum = float(ambient.get("illuminance", 0)) >= 5.0 and float(sky.get("intensity", 0)) >= 1.0
            except Exception:
                cave_maximum = False
            if not cave_maximum:
                self.report("warning", "洞窟の最低環境光が最大ではありません", "最低環境光5.0または空の間接光1.0を確認できません。", "「洞窟・暗所を最大環境光にする」をONにして調整を適用してください。", "CAVE_AMBIENT_NOT_MAXIMUM")
                return
        self.report("success", "水・洞窟とも最大設定です", "水透明度100%、水中フォグ・散乱・吸収0、洞窟最低環境光5.0、空の間接光1.0を確認しました。", "このままMinecraftを起動できます。", "WATER_CAVE_MAXIMUM_OK")

    def select_pack(self, pack: PackInfo | None) -> None:
        self.selected = pack
        if pack:
            self.current_name_var.set(pack.label.split(" — ", 1)[0])
            # Keep the full path internal, but show a compact, user-facing location.
            location = "development_resource_packs" if "development_resource_packs" in str(pack.path) else pack.location
            self.current_path_var.set(f"保存場所：{location}")
        else:
            self.current_name_var.set("パックが選択されていません")
            self.current_path_var.set("「最新版をインストール」を押してください。")
        self.update_button_states()

    def on_pack_selected(self, _event=None) -> None:
        index = self.pack_combo.current()
        self.select_pack(self.packs[index] if 0 <= index < len(self.packs) else None)
        self.run_diagnosis()

    def select_folder(self) -> None:
        folder = filedialog.askdirectory(title="manifest.jsonが入っているリソースパックを選択")
        if not folder:
            return
        path = Path(folder)
        try:
            manifest = read_json(path / "manifest.json")
            header = manifest.get("header", {})
            pack = PackInfo(str(header.get("name") or path.name), path, path.name, str(header.get("uuid") or path), list(header.get("version") or [0, 0, 0]), "手動選択", "manual")
        except Exception as error:
            self.finish_error("folder", error)
            return
        self.packs.insert(0, pack)
        self.pack_combo["values"] = [item.label for item in self.packs]
        self.pack_combo.current(0)
        self.select_pack(pack)
        self.run_diagnosis()

    def clear_selection(self) -> None:
        self.pack_var.set("")
        self.select_pack(None)
        self.report("info", "選択を解除しました", "現在は調整対象がありません。", "上の対象パックから選択してください。", "NO_PACK_SELECTED")

    def apply_gpu_profile(self, report_change: bool = True, reset_night_vision: bool = False) -> None:
        gpu = self.gpu_var.get()
        profile = GPU_PROFILES.get(gpu, GPU_PROFILES[DEFAULT_GPU])
        self.fog_var.set(profile["fog"]); self.emissive_var.set(profile["emissive"])
        self.relief_var.set(profile["relief"]); self.density_var.set(profile["density"])
        self.mirror_var.set(profile["mirror"]); self.roughness_var.set(profile["roughness"])
        self.water_var.set(profile["water_transparency"])
        self.ambient_var.set(profile["ambient"]); self.anti_var.set(profile["anti_flicker"])
        if reset_night_vision:
            self.night_vision_var.set(True)
        if report_change:
            self.report(
                "info",
                f"{gpu}向け推奨値に変更しました",
                "画面上の推奨値だけを変更しました。Minecraftのパックはまだ変更していません。",
                "反映するには「選択パックに調整を適用」を押してください。",
                "GPU_PROFILE_CHANGED",
            )

    def on_gpu_selected(self, _event=None) -> None:
        self.apply_gpu_profile(True)

    def reset_values(self) -> None:
        self.apply_gpu_profile(True, reset_night_vision=True)

    def update_requirements(self, manifest: dict) -> tuple[bool, bool]:
        app_needed = version_tuple(manifest["app"]["version"]) > version_tuple(APP_VERSION_NUMBER)
        pack = manifest["pack"]
        pack_name = str(pack.get("name", ""))
        pack_file_name = Path(str(pack.get("file_name", ""))).name
        installed_versions = [version_tuple(item.version) for item in self.packs if item.name == pack_name]
        installed = bool(installed_versions)
        asset_exists = bool(pack_file_name) and (BASE_DIR / "assets" / pack_file_name).is_file()
        required_pack_version = version_tuple(pack.get("version", "0"))
        installed_pack_version = max(installed_versions, default=(0,))
        pack_needed = (
            int(pack["number"]) > PACK_NUMBER
            or not installed
            or not asset_exists
            or installed_pack_version < required_pack_version
        )
        return app_needed, pack_needed

    def automatic_update_check(self) -> None:
        if self.busy:
            self.root.after(2500, self.automatic_update_check)
            return
        def worker():
            try:
                manifest = fetch_update_manifest()
            except Exception:
                return
            self.root.after(0, lambda value=manifest: self.show_automatic_update(value))
        threading.Thread(target=worker, daemon=True).start()

    def show_automatic_update(self, manifest: dict) -> None:
        if self.busy:
            return
        app_needed, pack_needed = self.update_requirements(manifest)
        if not (app_needed or pack_needed):
            return
        pack_number = manifest["pack"]["number"]
        self.update_button.configure(text=f"⇩ 更新があります：アプリ {manifest['app']['version']}・！{pack_number}")
        self.report(
            "info",
            "GitHubに更新があります",
            f"アプリ {manifest['app']['version']} / リソースパック！{pack_number}",
            "画面下の更新ボタンを押すと、取得からMinecraftへの導入まで自動で行います。",
            "UPDATE_AVAILABLE",
        )

    def check_for_updates(self) -> None:
        def worker():
            return fetch_update_manifest()
        def success(manifest):
            app_needed, pack_needed = self.update_requirements(manifest)
            if not (app_needed or pack_needed):
                self.update_button.configure(text="✓ アプリ・リソースパックは最新版です")
                self.set_status(100, "最新版を使用しています。")
                self.report("success", "最新版です", f"アプリ {APP_VERSION_NUMBER} / リソースパック！{PACK_NUMBER}", "追加のダウンロードは必要ありません。", "UPDATE_NOT_NEEDED")
                messagebox.showinfo("最新版です", "アプリとリソースパックは最新版です。")
                return
            targets = []
            if app_needed:
                targets.append(f"アプリ {manifest['app']['version']}")
            if pack_needed:
                targets.append(f"リソースパック！{manifest['pack']['number']}")
            if messagebox.askyesno("更新が見つかりました", "次の更新をGitHubから取得して自動導入します。\n\n" + "\n".join(f"・{item}" for item in targets) + "\n\nMinecraftを完全に終了してから「はい」を押してください。"):
                self.download_and_apply_updates(manifest, app_needed, pack_needed)
        self.start_task("update", "GitHubで最新版を確認しています", worker, success)

    def download_and_apply_updates(self, manifest: dict, app_needed: bool, pack_needed: bool) -> None:
        preview = self.preview_var.get()
        def worker():
            update_root = manager_home() / "Updates"
            update_root.mkdir(parents=True, exist_ok=True)
            result = {"app_updated": False, "pack_installed": None, "backup": None}
            pack_info = manifest["pack"]
            pack_file_name = Path(str(pack_info["file_name"])).name
            if not pack_file_name.lower().endswith(".mcpack"):
                raise ValueError("更新パックのファイル名が不正です。")
            pack_asset = BASE_DIR / "assets" / pack_file_name
            must_fetch_pack = pack_needed or not pack_asset.is_file()
            if must_fetch_pack:
                self.thread_status(8, f"リソースパック！{pack_info['number']}を取得しています…")
                downloaded_pack = download_verified_file(pack_info, update_root / pack_file_name, MAX_PACK_BYTES, self.thread_status)
                with zipfile.ZipFile(downloaded_pack) as archive:
                    bad = archive.testzip()
                    if bad:
                        raise ValueError(f"更新パックが壊れています: {bad}")
                pack_asset.parent.mkdir(parents=True, exist_ok=True)
                temporary_asset = pack_asset.with_name(pack_asset.name + ".new")
                shutil.copy2(downloaded_pack, temporary_asset)
                os.replace(temporary_asset, pack_asset)
            if pack_needed:
                self.thread_status(84, f"リソースパック！{pack_info['number']}をMinecraftへ導入しています…")
                result["pack_installed"] = install_pack_archive(
                    pack_asset,
                    preview,
                    str(pack_info["folder"]),
                    str(pack_info.get("header_uuid") or "") or None,
                )
            if app_needed:
                app_info = manifest["app"]
                app_file_name = Path(str(app_info["file_name"])).name
                if not app_file_name.lower().endswith(".pyw"):
                    raise ValueError("更新アプリのファイル名が不正です。")
                self.thread_status(90, f"アプリ {app_info['version']}を取得しています…")
                downloaded_app = download_verified_file(app_info, update_root / app_file_name, MAX_APP_BYTES, self.thread_status)
                result["backup"] = apply_python_update(downloaded_app, str(app_info["version"]))
                result["app_updated"] = True
            return result
        def success(result):
            self.refresh_packs()
            self.update_button.configure(text="✓ 更新が完了しました")
            details = []
            if result["pack_installed"]:
                details.append("最新リソースパックをMinecraftへ導入しました。")
            if result["app_updated"]:
                details.append("Pythonアプリを更新し、以前の版をバックアップしました。")
            message = "\n".join(details) or "更新ファイルを確認しました。"
            self.report("success", "自動更新が完了しました", message, "アプリ更新後は再起動してください。", "UPDATE_OK")
            self.set_status(100, "自動更新が完了しました。")
            if result["app_updated"] and messagebox.askyesno("再起動が必要です", message + "\n\n今すぐアプリを再起動しますか？"):
                try:
                    subprocess.Popen([sys.executable, str(Path(__file__).resolve())], close_fds=True)
                except Exception as error:
                    self.finish_error("update", error)
                    return
                self.root.destroy()
            elif not result["app_updated"]:
                messagebox.showinfo("更新できました", message + "\n\nMinecraftで最新パックを有効化してください。")
        self.start_task("update", "最新版を取得して自動導入しています", worker, success)

    def install_pack(self) -> None:
        preview = self.preview_var.get()
        def worker():
            if not EMBEDDED_PACK.is_file():
                raise FileNotFoundError(EMBEDDED_PACK)
            return install_pack_archive(EMBEDDED_PACK, preview, PACK_FOLDER)
        def success(destination):
            self.refresh_packs()
            self.report("success", "最新版をインストールしました", str(destination), "Minecraftで古い！33を無効化し、！34を最上位で有効化してください。", "INSTALL_OK")
            messagebox.showinfo("導入できました", "水透明度100%・洞窟環境光MAX版！34を導入しました。\n\nMinecraftで古い！33を無効化し、！34を最上位で有効化してください。")
        self.start_task("install", "最新版をインストールしています", worker, success)

    def apply_tuning(self) -> None:
        if not self.selected:
            return
        if not messagebox.askyesno("調整を適用します", "Minecraftを完全に終了していますか？\n\n変更前を自動保存してから調整します。"):
            return
        pack = self.selected
        settings = self.settings()
        def worker():
            baseline = baseline_path(pack)
            had_baseline = (baseline / "manifest.json").is_file()
            self.thread_status(8, "変更前のパックをバックアップしています…")
            ensure_baseline(pack)
            if had_baseline:
                restore_baseline(pack)
            copy_relief_preset(pack.path, settings["relief"])
            heightmaps = list((pack.path / "textures" / "blocks").rglob("*_adjustable_heightmap.png"))
            for index, path in enumerate(heightmaps):
                tune_heightmap(path, settings["density"], settings["anti_flicker"])
                if index % 60 == 0:
                    self.thread_status(15 + 20 * index / max(1, len(heightmaps)), f"凹凸を調整しています… {index}/{len(heightmaps)}")
            self.thread_status(36, "ライトブロック0～15の標準表示を復旧しています…")
            ensure_light_block_assets(pack.path)
            tune_texture_sets(pack.path, settings, self.thread_status)
            self.thread_status(80, "水の透明度を調整しています…")
            water_files = tune_water_transparency(pack.path, settings["water_transparency"], self.thread_status)
            self.thread_status(88, "陸上・水中の霧、照明、影を調整しています…")
            tune_fog_lighting(pack.path, settings)
            write_json(
                pack.path / "my_original_rtx_manager_settings.json",
                {
                    "app": APP_NAME,
                    "app_version": APP_VERSION,
                    "applied_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "settings": settings,
                    "protections": {
                        "official_16px_rgb_untouched": True,
                        "water_alpha_adjusted": water_files > 0,
                        "water_transparency_100_percent": settings["water_transparency"] >= 100,
                        "underwater_fog_density_zero": settings["water_transparency"] >= 100,
                        "underwater_scattering_absorption_zero": settings["water_transparency"] >= 100,
                        "grass_side_black_fix_preserved": True,
                        "calcite_quartz_non_mirror": True,
                        "light_blocks_0_to_15_emissive": True,
                        "light_block_standard_held_item_visibility": True,
                        "deprecated_light_block_shape_removed": True,
                        "light_block_rtx_blockshape_workaround": False,
                        "light_block_vibrant_visuals_static_light": True,
                        "ray_traced_invisible_point_light_supported": False,
                        "cave_night_vision": bool(settings["night_vision"]),
                        "cave_ambient_illuminance_max_5": bool(settings["night_vision"]),
                        "sky_indirect_intensity_max_1": bool(settings["night_vision"]),
                    },
                },
            )
            return pack
        def success(_):
            self.set_status(100, "調整が完了しました。")
            cave_light = "最大5.0" if settings["night_vision"] else "通常"
            self.report("success", "設定の適用が完了しました", f"水透明度: {settings['water_transparency']}% / 洞窟最低環境光: {cave_light}", "Minecraftを完全に終了してから起動し、！33を無効化・！34を最上位で有効化してワールドへ入り直してください。", "APPLY_OK")
            messagebox.showinfo("調整できました", f"水の透明度: {settings['water_transparency']}%\n水中フォグ・散乱・吸収: 0（100%時）\n洞窟最低環境光: {cave_light}\n空の間接光: 最大1.0\n\nMinecraftを完全に終了してから起動し、！33を無効化して！34を最上位で有効化してください。")
        self.start_task("apply", "設定を適用しています", worker, success)

    def restore_pack(self) -> None:
        if not self.selected or not messagebox.askyesno("元に戻します", "選択パックを調整前の状態へ戻しますか？"):
            return
        pack = self.selected
        def worker():
            restore_baseline(pack)
            return pack
        def success(_):
            self.refresh_packs()
            self.report("success", "復元が完了しました", "選択パックを調整前へ戻しました。", "Minecraftでワールドへ入り直してください。", "RESTORE_OK")
        self.start_task("restore", "バックアップから復元しています", worker, success)

    def export_pack(self) -> None:
        if not self.selected:
            return
        output = filedialog.asksaveasfilename(title="調整済みパックを書き出す", defaultextension=".mcpack", initialfile=f"{self.selected.folder}_Custom.mcpack", filetypes=[("Minecraft Resource Pack", "*.mcpack")])
        if not output:
            return
        pack = self.selected
        target = Path(output)
        def worker():
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for path in pack.path.rglob("*"):
                    if path.is_file():
                        archive.write(path, path.relative_to(pack.path).as_posix())
            with zipfile.ZipFile(target) as archive:
                bad = archive.testzip()
                if bad:
                    raise ValueError(f"書き出したパックが壊れています: {bad}")
            return target
        def success(path):
            self.set_status(100, "mcpackを書き出しました。")
            self.report("success", "書き出しが完了しました", str(path), "このmcpackは別のPCでも導入できます。", "EXPORT_OK")
            messagebox.showinfo("書き出しました", str(path))
        self.start_task("export", "パックを書き出しています", worker, success)

    def delete_pack(self) -> None:
        if not self.selected or not messagebox.askyesno("パックを削除します", f"{self.selected.name}\n\nWindowsのゴミ箱へ移動しますか？"):
            return
        pack = self.selected
        def worker():
            send_to_recycle_bin(pack.path)
            return pack
        def success(deleted):
            self.selected = None
            self.refresh_packs()
            self.report("success", "パックをゴミ箱へ移動しました", deleted.name, "必要ならWindowsのゴミ箱から戻せます。", "DELETE_OK")
        self.start_task("delete", "パックをゴミ箱へ移動しています", worker, success)

    def open_pack_folder(self) -> None:
        if not self.selected:
            return
        try:
            os.startfile(str(self.selected.path))
            self.report("info", "パックフォルダーを開きました", str(self.selected.path), "直接変更する場合はバックアップを残してください。", "FOLDER_OPENED")
        except Exception as error:
            self.finish_error("scan", error)

    def launch_minecraft(self) -> None:
        try:
            os.startfile("minecraft://")
            self.report("success", "Minecraftを起動しました", "Minecraftへ起動命令を送りました。", "！33を無効化し、！34を最上位で有効化してください。", "MINECRAFT_STARTED")
        except Exception:
            try:
                subprocess.Popen(["cmd", "/c", "start", "", "minecraft://"], shell=False)
            except Exception as error:
                self.finish_error("launch", error)


def enable_high_dpi() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def configure_tk_quality(root: tk.Tk) -> None:
    try:
        dpi = float(root.winfo_fpixels("1i"))
        root.tk.call("tk", "scaling", max(1.0, min(2.5, dpi / 72.0)))
    except Exception:
        pass
    root.option_add("*Font", ("Yu Gothic UI", 10))
    root.option_add("*tearOff", False)


def main() -> None:
    enable_high_dpi()
    root = tk.Tk()
    configure_tk_quality(root)
    ManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
