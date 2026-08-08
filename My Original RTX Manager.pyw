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
APP_VERSION_NUMBER = "2.3.0"
APP_VERSION = f"{APP_VERSION_NUMBER} Python"
PACK_NUMBER = 31
PACK_FOLDER = "My_Original_Visual_Pack_31"
PACK_NAME = "My Original Visual Pack！31 水中暗視級透明度版"
BASE_DIR = Path(__file__).resolve().parent
EMBEDDED_PACK = BASE_DIR / "assets" / "My_Original_Visual_Pack_31.mcpack"
UPDATE_REPOSITORY = "koresuke26/my-original-rtx-manager-updates"
UPDATE_RAW_ROOT = f"https://raw.githubusercontent.com/{UPDATE_REPOSITORY}/main"
UPDATE_MANIFEST_URL = f"{UPDATE_RAW_ROOT}/latest.json"
ALLOWED_UPDATE_HOSTS = {"raw.githubusercontent.com"}
MAX_MANIFEST_BYTES = 256 * 1024
MAX_APP_BYTES = 5 * 1024 * 1024
MAX_PACK_BYTES = 100 * 1024 * 1024

DEFAULTS = {
    "fog": 35,
    "emissive": 100,
    "relief": 3,
    "density": 3,
    "mirror": 90,
    "roughness": 45,
    "water_transparency": 85,
    "ambient": False,
    "anti_flicker": True,
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
        return f"{self.name} — {self.location}"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
            if not re.fullmatch(r"textures/items/light_block_(?:[0-9]|1[0-5])(?:\.png|_mers\.tga|\.texture_set\.json)", name):
                continue
            target = pack_path / Path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))


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
        lighting = read_json(lighting_path)
        entry = lighting.get("minecraft:lighting_settings", {})
        if isinstance(entry.get("ambient"), dict):
            entry["ambient"]["illuminance"] = 0.022 if settings["ambient"] else 0.004
        if isinstance(entry.get("sky"), dict):
            entry["sky"]["intensity"] = 0.42 if settings["ambient"] else 0.15
        if isinstance(entry.get("emissive"), dict):
            entry["emissive"]["desaturation"] = 0.0
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
        self.root.geometry("1280x760")
        self.root.minsize(680, 520)
        self.root.configure(bg=self.BG)
        self.packs: list[PackInfo] = []
        self.selected: PackInfo | None = None
        self.scan_issues: list[tuple[Path, str]] = []
        self.diagnostics: list[str] = []
        self.busy = False
        self.compact = False
        self.action_buttons: list[ttk.Button] = []

        self.preview_var = tk.BooleanVar(value=False)
        self.pack_var = tk.StringVar()
        self.fog_var = tk.DoubleVar(value=DEFAULTS["fog"])
        self.emissive_var = tk.DoubleVar(value=DEFAULTS["emissive"])
        self.relief_var = tk.DoubleVar(value=DEFAULTS["relief"])
        self.density_var = tk.DoubleVar(value=DEFAULTS["density"])
        self.mirror_var = tk.DoubleVar(value=DEFAULTS["mirror"])
        self.roughness_var = tk.DoubleVar(value=DEFAULTS["roughness"])
        self.water_var = tk.DoubleVar(value=DEFAULTS["water_transparency"])
        self.ambient_var = tk.BooleanVar(value=DEFAULTS["ambient"])
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
        style.configure("Header.TLabel", background="#292e30", foreground=self.TEXT, font=("Yu Gothic UI", 18, "bold"))
        style.configure("HeaderSub.TLabel", background="#292e30", foreground=self.MUTED, font=("Yu Gothic UI", 9))
        style.configure("TButton", background=self.PANEL2, foreground=self.TEXT, bordercolor=self.BORDER, padding=(12, 9), font=("Yu Gothic UI", 10, "bold"))
        style.map("TButton", background=[("active", "#394143"), ("disabled", "#25292a")], foreground=[("disabled", "#707978")])
        style.configure("Primary.TButton", background=self.GREEN_DARK, foreground="white", bordercolor=self.GREEN, padding=(14, 10))
        style.map("Primary.TButton", background=[("active", "#3ca66d")])
        style.configure("Danger.TButton", background="#3a2a2d", foreground="#ffd4d6", bordercolor="#8f454b")
        style.configure("TCheckbutton", background=self.PANEL, foreground=self.TEXT, font=("Yu Gothic UI", 10))
        style.map("TCheckbutton", background=[("active", self.PANEL)])
        style.configure("TCombobox", fieldbackground="#1e2223", background=self.PANEL2, foreground=self.TEXT, arrowcolor=self.TEXT, bordercolor=self.BORDER, padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", "#1e2223")], foreground=[("readonly", self.TEXT)])
        style.configure("Horizontal.TScale", background=self.PANEL, troughcolor="#777e7f", sliderrelief="flat")
        style.configure("Horizontal.TProgressbar", troughcolor="#303638", background=self.GREEN, bordercolor="#303638")
        style.configure("TLabelframe", background=self.PANEL, foreground=self.TEXT, bordercolor=self.BORDER, relief="solid")
        style.configure("TLabelframe.Label", background=self.PANEL, foreground=self.GREEN, font=("Yu Gothic UI", 10, "bold"))

    def build_ui(self) -> None:
        header = tk.Frame(self.root, bg="#292e30", height=66)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        ttk.Label(header, text="▦", style="Header.TLabel", foreground=self.GREEN).pack(side="left", padx=(20, 10))
        titles = tk.Frame(header, bg="#292e30")
        titles.pack(side="left", pady=10)
        ttk.Label(titles, text=APP_NAME, style="Header.TLabel").pack(anchor="w")
        ttk.Label(titles, text="Bedrock RTX・Vibrant Visuals パック管理（Python版）", style="HeaderSub.TLabel").pack(anchor="w")
        ttk.Label(header, text="RTX 3080向け", style="HeaderSub.TLabel", foreground="#c9ffe0").pack(side="right", padx=22)

        toolbar = tk.Frame(self.root, bg="#202426", padx=14, pady=9)
        toolbar.grid(row=1, column=0, sticky="ew")
        self.select_button = ttk.Button(toolbar, text="⌕ 別のパックを選択", command=self.select_folder)
        self.select_button.grid(row=0, column=0, padx=(0, 7), sticky="ew")
        self.preview_button = ttk.Checkbutton(toolbar, text="Minecraft Preview", variable=self.preview_var, command=self.refresh_packs)
        self.preview_button.grid(row=0, column=1, padx=7)
        self.pack_combo = ttk.Combobox(toolbar, textvariable=self.pack_var, state="readonly")
        self.pack_combo.grid(row=0, column=2, padx=7, sticky="ew")
        self.pack_combo.bind("<<ComboboxSelected>>", self.on_pack_selected)
        self.refresh_button = ttk.Button(toolbar, text="↻", width=3, command=self.refresh_packs)
        self.refresh_button.grid(row=0, column=3, padx=(7, 0))
        toolbar.columnconfigure(2, weight=1)

        self.canvas = tk.Canvas(self.root, bg=self.BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.root, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.grid(row=2, column=0, sticky="nsew")
        scrollbar.grid(row=2, column=1, sticky="ns")
        self.content = tk.Frame(self.canvas, bg=self.BG, padx=14, pady=14)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        self.canvas.bind_all("<MouseWheel>", lambda event: self.canvas.yview_scroll(int(-event.delta / 120), "units"))

        self.left = ttk.Frame(self.content, style="Panel.TFrame", padding=16)
        self.right = ttk.Frame(self.content, style="Panel.TFrame", padding=16)
        self.left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.right.grid(row=0, column=1, sticky="nsew")
        self.content.columnconfigure(1, weight=1)
        self.build_left()
        self.build_right()

        status = tk.Frame(self.root, bg="#202426", padx=14, pady=7)
        status.grid(row=3, column=0, columnspan=2, sticky="ew")
        ttk.Progressbar(status, variable=self.progress_var, maximum=100).pack(fill="x")
        tk.Label(status, textvariable=self.status_var, bg="#202426", fg=self.MUTED, font=("Yu Gothic UI", 9)).pack(pady=(4, 0))

        self.root.rowconfigure(2, weight=1)
        self.root.columnconfigure(0, weight=1)

    def build_left(self) -> None:
        ttk.Label(self.left, text="CURRENT PACK", style="Muted.TLabel", foreground=self.GREEN).pack(anchor="w")
        ttk.Label(self.left, textvariable=self.current_name_var, style="Title.TLabel", wraplength=310).pack(anchor="w", pady=(5, 4))
        ttk.Label(self.left, textvariable=self.current_path_var, style="Muted.TLabel", wraplength=310).pack(anchor="w")

        protection = ttk.LabelFrame(self.left, text="✓ 保護される設定", padding=10)
        protection.pack(fill="x", pady=12)
        ttk.Label(
            protection,
            text="• 公式16pxのRGB色を維持\n• 水だけ透明度を調整可能\n• 草側面の黒化防止\n• 方解石・クォーツは非鏡面\n• ライトブロック0～15を発光対応\n• GitHubから安全確認後に自動更新\n• 変更前に自動バックアップ",
            style="Panel.TLabel",
            justify="left",
        ).pack(anchor="w")

        self.diag_frame = ttk.LabelFrame(self.left, text="トラブル診断", padding=10)
        self.diag_frame.pack(fill="x", pady=(0, 10))
        self.diag_title = tk.Label(self.diag_frame, textvariable=self.diag_title_var, bg=self.PANEL, fg=self.BLUE, font=("Yu Gothic UI", 11, "bold"), anchor="w")
        self.diag_title.pack(fill="x")
        ttk.Label(self.diag_frame, textvariable=self.diag_message_var, style="Panel.TLabel", wraplength=295, justify="left").pack(anchor="w", pady=(5, 4))
        ttk.Label(self.diag_frame, text="対処方法", style="Muted.TLabel", foreground=self.GREEN).pack(anchor="w")
        ttk.Label(self.diag_frame, textvariable=self.diag_action_var, style="Muted.TLabel", wraplength=295, justify="left").pack(anchor="w")
        meta = ttk.Frame(self.diag_frame, style="Panel.TFrame")
        meta.pack(fill="x", pady=(6, 4))
        ttk.Label(meta, textvariable=self.diag_code_var, style="Muted.TLabel").pack(side="left")
        ttk.Label(meta, textvariable=self.diag_time_var, style="Muted.TLabel").pack(side="right")
        ttk.Button(self.diag_frame, text="↻ 今すぐ診断", command=self.refresh_packs).pack(fill="x", pady=(2, 5))
        self.history = tk.Listbox(self.diag_frame, height=4, bg="#1d2122", fg=self.MUTED, selectbackground=self.GREEN_DARK, borderwidth=0, font=("Yu Gothic UI", 8))
        self.history.pack(fill="x")
        ttk.Label(self.left, text=f"App Version  {APP_VERSION}", style="Muted.TLabel").pack(anchor="e", pady=(4, 0))

    def build_right(self) -> None:
        quick = ttk.Frame(self.right, style="Panel.TFrame")
        quick.pack(fill="x", pady=(0, 10))
        ttk.Button(quick, text="⌫ 選択解除", command=self.clear_selection).pack(side="right", padx=(7, 0))
        ttk.Button(quick, text="↶ 推奨値に戻す", command=self.reset_values).pack(side="right")

        settings = ttk.Frame(self.right, style="Panel.TFrame")
        settings.pack(fill="x")
        left_settings = ttk.LabelFrame(settings, text="凹凸・霧・発光", padding=12)
        right_settings = ttk.LabelFrame(settings, text="反射・見やすさ", padding=12)
        left_settings.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        right_settings.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        settings.columnconfigure((0, 1), weight=1)
        self.add_slider(left_settings, "霧の濃さ", self.fog_var, 0, 100, "右ほど独自の霧が濃くなります", "")
        self.add_slider(left_settings, "発光の明るさ", self.emissive_var, 0, 200, "光源とライトブロックの発光を調整", "%")
        self.add_slider(left_settings, "凹凸の強さ", self.relief_var, 1, 5, "公式色は変えず立体感だけ調整", " / 5")
        self.add_slider(left_settings, "凹凸の密度", self.density_var, 1, 5, "凹凸が現れる場所の多さ", " / 5")
        self.add_slider(right_settings, "金属・宝石の鏡面反射", self.mirror_var, 0, 100, "色を残したまま反射を変更", "%")
        self.add_slider(right_settings, "表面の粗さ", self.roughness_var, 0, 100, "右ほど落ち着いた反射", "")
        self.add_slider(right_settings, "水の透明度", self.water_var, 0, 100, "100%で暗視ポーション級の鮮明さ", "%")
        ttk.Checkbutton(right_settings, text="環境光を強くする（暗部を見やすく）", variable=self.ambient_var).pack(anchor="w", pady=15)
        ttk.Checkbutton(right_settings, text="ちらつき防止（推奨：ON）", variable=self.anti_var).pack(anchor="w", pady=15)

        apply_row = ttk.Frame(self.right, style="Panel.TFrame")
        apply_row.pack(fill="x", pady=12)
        self.apply_button = ttk.Button(apply_row, text="⚙ 選択パックに調整を適用", style="Primary.TButton", command=self.apply_tuning)
        self.apply_button.pack(side="left", fill="x", expand=True)
        self.export_button = ttk.Button(apply_row, text="⇩ 書き出す", command=self.export_pack)
        self.export_button.pack(side="left", padx=8)
        self.delete_button = ttk.Button(apply_row, text="♲", style="Danger.TButton", width=3, command=self.delete_pack)
        self.delete_button.pack(side="left")

        actions = ttk.Frame(self.right, style="Panel.TFrame")
        actions.pack(fill="x")
        self.install_button = ttk.Button(actions, text="▣ 最新の！31を導入\n水中暗視級・ライト発光修正版", command=self.install_pack)
        self.restore_button = ttk.Button(actions, text="↶ バックアップから復元\n調整前へ戻す", command=self.restore_pack)
        self.folder_button = ttk.Button(actions, text="▤ パックフォルダーを開く", command=self.open_pack_folder)
        self.launch_button = ttk.Button(actions, text="▶ Minecraft RTXを起動", style="Primary.TButton", command=self.launch_minecraft)
        for index, button in enumerate((self.install_button, self.restore_button, self.folder_button, self.launch_button)):
            button.grid(row=index // 2, column=index % 2, sticky="ew", padx=(0 if index % 2 == 0 else 5, 5 if index % 2 == 0 else 0), pady=5)
        self.update_button = ttk.Button(actions, text="⇩ GitHubで最新版を確認・自動更新", style="Primary.TButton", command=self.check_for_updates)
        self.update_button.grid(row=2, column=0, columnspan=2, sticky="ew", pady=5)
        actions.columnconfigure((0, 1), weight=1)
        self.action_buttons = [self.select_button, self.refresh_button, self.apply_button, self.export_button, self.delete_button, self.install_button, self.restore_button, self.folder_button, self.launch_button, self.update_button]
        self.update_button_states()

    def add_slider(self, parent, title: str, variable: tk.DoubleVar, start: int, end: int, note: str, suffix: str) -> None:
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill="x", pady=8)
        value_var = tk.StringVar()
        def update(*_):
            value = int(round(variable.get()))
            value_var.set(f"{value}{suffix}")
        variable.trace_add("write", update)
        update()
        top = ttk.Frame(row, style="Panel.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text=title, style="Panel.TLabel", font=("Yu Gothic UI", 10, "bold")).pack(side="left")
        ttk.Label(top, textvariable=value_var, style="Panel.TLabel").pack(side="right")
        step = 1 if end <= 5 else 10 if end == 200 else 5
        scale = ttk.Scale(
            row,
            from_=start,
            to=end,
            variable=variable,
            command=lambda raw, var=variable, amount=step: var.set(round(float(raw) / amount) * amount),
        )
        scale.pack(fill="x", pady=(5, 2))
        ttk.Label(row, text=note, style="Muted.TLabel").pack(anchor="w")

    def on_canvas_resize(self, event) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=max(640, event.width))
        compact = event.width < 900
        if compact == self.compact:
            return
        self.compact = compact
        if compact:
            self.left.grid_configure(row=0, column=0, columnspan=2, padx=0, pady=(0, 12))
            self.right.grid_configure(row=1, column=0, columnspan=2)
            self.content.columnconfigure(0, weight=1)
            self.content.columnconfigure(1, weight=0)
        else:
            self.left.grid_configure(row=0, column=0, columnspan=1, padx=(0, 12), pady=0)
            self.right.grid_configure(row=0, column=1, columnspan=1)
            self.content.columnconfigure(0, weight=0)
            self.content.columnconfigure(1, weight=1)

    def settings(self) -> dict:
        return {
            "fog": int(self.fog_var.get()), "emissive": int(self.emissive_var.get()),
            "relief": int(self.relief_var.get()), "density": int(self.density_var.get()),
            "mirror": int(self.mirror_var.get()), "roughness": int(self.roughness_var.get()),
            "water_transparency": int(self.water_var.get()),
            "ambient": bool(self.ambient_var.get()), "anti_flicker": bool(self.anti_var.get()),
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
            "install": "！31の導入", "apply": "設定の適用", "restore": "復元",
            "export": "書き出し", "delete": "削除", "scan": "パック検索",
            "folder": "フォルダー表示", "launch": "Minecraftの起動", "update": "自動更新",
        }.get(context, "処理")
        if "backup" in lowered or "バックアップ" in raw:
            return "BACKUP_NOT_FOUND", "バックアップがありません", f"{operation}を続けられませんでした。", "先に一度「選択パックに調整を適用」を押してください。"
        if isinstance(error, PermissionError) or "access is denied" in lowered or "permission" in lowered:
            return "ACCESS_DENIED", "ファイルを変更できません", f"{operation}中にWindowsから拒否されました。", "Minecraftを完全に終了して10秒ほど待ち、もう一度実行してください。"
        if isinstance(error, FileNotFoundError):
            return "FILE_NOT_FOUND", "必要なファイルがありません", raw, "Minecraftを一度起動して終了し、再診断してください。パックの場合は！31を入れ直してください。"
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
        packs.sort(key=lambda pack: (0 if "！31" in pack.name else 1, pack.name))
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
            self.set_status(0, "PBRパックが見つかりません。先に！31を導入してください。")
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
            self.report("warning", "RTX対応パックがありません", "Minecraftの保存場所は正常です。", "「最新の！31を導入」を押してください。", "PBR_PACK_NOT_FOUND")
            return
        if self.selected:
            light_set = self.selected.path / "textures" / "items" / "light_block_15.texture_set.json"
            try:
                entry = read_json(light_set).get("minecraft:texture_set", {})
                fixed = "metalness_emissive_roughness" in entry
            except Exception:
                fixed = False
            if not fixed:
                self.report("warning", "ライトブロックが未修正です", "選択中のパックにはRTX用ライトブロック発光設定がありません。", "「選択パックに調整を適用」を押すと、自動でライト0～15を修正します。", "LIGHT_BLOCK_FIX_MISSING")
                return
            fog_path = self.selected.path / "fogs" / "overworld_fog.json"
            try:
                fog_entry = read_json(fog_path).get("minecraft:fog_settings", {})
                water_distance = fog_entry.get("distance", {}).get("water")
                water_density = fog_entry.get("volumetric", {}).get("density", {}).get("water")
                water_fixed = isinstance(water_distance, dict) and isinstance(water_density, dict)
            except Exception:
                water_fixed = False
            if not water_fixed:
                self.report("warning", "水中の透明度が未設定です", "選択中のパックには水中専用の見通し設定がありません。", "水の透明度を選び、「選択パックに調整を適用」を押してください。", "WATER_CLARITY_MISSING")
                return
        self.report("success", "問題は見つかりません", f"{len(self.packs)}個のPBRパック、ライトブロック、水中透明度設定を確認しました。", "このまま設定を調整できます。", "SYSTEM_OK")

    def select_pack(self, pack: PackInfo | None) -> None:
        self.selected = pack
        self.current_name_var.set(pack.name if pack else "パックが選択されていません")
        self.current_path_var.set(str(pack.path) if pack else "「最新の！31を導入」を押してください。")
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

    def reset_values(self) -> None:
        self.fog_var.set(DEFAULTS["fog"]); self.emissive_var.set(DEFAULTS["emissive"])
        self.relief_var.set(DEFAULTS["relief"]); self.density_var.set(DEFAULTS["density"])
        self.mirror_var.set(DEFAULTS["mirror"]); self.roughness_var.set(DEFAULTS["roughness"])
        self.water_var.set(DEFAULTS["water_transparency"])
        self.ambient_var.set(DEFAULTS["ambient"]); self.anti_var.set(DEFAULTS["anti_flicker"])
        self.report("info", "推奨値に戻しました", "RTX 3080向けの標準値へ戻しました。", "反映するには「選択パックに調整を適用」を押してください。", "VALUES_RESET")

    def update_requirements(self, manifest: dict) -> tuple[bool, bool]:
        app_needed = version_tuple(manifest["app"]["version"]) > version_tuple(APP_VERSION_NUMBER)
        pack = manifest["pack"]
        pack_name = str(pack.get("name", ""))
        pack_file_name = Path(str(pack.get("file_name", ""))).name
        installed = any(item.name == pack_name for item in self.packs)
        asset_exists = bool(pack_file_name) and (BASE_DIR / "assets" / pack_file_name).is_file()
        pack_needed = int(pack["number"]) > PACK_NUMBER or not installed or not asset_exists
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
            self.report("success", "！31を導入しました", str(destination), "Minecraftで古い！30を無効化し、！31を有効化してください。", "INSTALL_OK")
            messagebox.showinfo("導入できました", "水中暗視級・ライトブロック発光修正版！31を導入しました。\n\nMinecraftで古い！30を無効化し、！31を有効化してください。")
        self.start_task("install", "！31を導入しています", worker, success)

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
            self.thread_status(36, "ライトブロック0～15を発光対応にしています…")
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
                        "underwater_fog_adjusted": True,
                        "grass_side_black_fix_preserved": True,
                        "calcite_quartz_non_mirror": True,
                        "light_blocks_0_to_15_emissive": True,
                    },
                },
            )
            return pack
        def success(_):
            self.set_status(100, "調整が完了しました。")
            self.report("success", "設定の適用が完了しました", "凹凸・反射・陸上霧・水の透明度・水中霧・照明・ライトブロックを調整しました。", "Minecraftでワールドへ入り直してください。", "APPLY_OK")
            messagebox.showinfo("調整できました", "水の透明度と水中の見通しを調整しました。\n100%では暗視ポーション級の設定になります。\nライトブロック0～15も発光対応済みです。\n\nMinecraftでワールドへ入り直してください。")
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
            self.report("success", "Minecraftを起動しました", "Minecraftへ起動命令を送りました。", "ビデオ設定でレイトレースを選び、！31を有効化してください。", "MINECRAFT_STARTED")
        except Exception:
            try:
                subprocess.Popen(["cmd", "/c", "start", "", "minecraft://"], shell=False)
            except Exception as error:
                self.finish_error("launch", error)


def main() -> None:
    root = tk.Tk()
    ManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
