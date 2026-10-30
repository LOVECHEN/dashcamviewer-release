#!/usr/bin/env python3
"""Mirror official Dashcam Viewer installers and support files to GitHub Releases."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


def required_url(name: str) -> str:
    value = os.environ.get(name, "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"{name} must be configured as an Actions Secret URL")
    return value


ROOT = Path(__file__).resolve().parents[1]
SITE_URL = required_url("DCV_SITE_URL")
OFFICIAL_BASE = required_url("DCV_DOWNLOAD_BASE_URL")
FREE_TRIAL_URL = f"{SITE_URL}/free-trial/"
GPS_CONVERTER_URL = f"{OFFICIAL_BASE}/Visiondrive_9600_Converter_v1.0.1.zip"


def log(message: str) -> None:
    print(message, flush=True)


def run(
    args: Iterable[str],
    *,
    check: bool = True,
    capture: bool = False,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=check,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def curl_text(url: str) -> str:
    result = run(
        (
            "curl",
            "-fsSL",
            "--retry",
            "5",
            "--retry-delay",
            "3",
            "--retry-all-errors",
            url,
        ),
        capture=True,
    )
    return result.stdout


def probe(url: str) -> bool:
    result = run(
        (
            "curl",
            "-fsSIL",
            "--retry",
            "3",
            "--retry-delay",
            "2",
            "--connect-timeout",
            "20",
            "--max-time",
            "60",
            "-o",
            "/dev/null",
            url,
        ),
        check=False,
        capture=True,
    )
    return result.returncode == 0


def download(url: str, destination: Path) -> None:
    part = destination.with_name(destination.name + ".part")
    part.unlink(missing_ok=True)
    log(f"  ↓ {destination.name}")
    try:
        run(
            (
                "curl",
                "-fL",
                "--retry",
                "6",
                "--retry-delay",
                "10",
                "--retry-all-errors",
                "--connect-timeout",
                "30",
                "--max-time",
                "7200",
                "-o",
                str(part),
                url,
            )
        )
        if part.stat().st_size == 0:
            raise RuntimeError(f"empty download: {destination.name}")
        part.replace(destination)
    finally:
        part.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path) -> None:
    suffix = path.suffix.lower()
    size = path.stat().st_size
    with path.open("rb") as handle:
        magic = handle.read(8)
    if suffix in {".dmg", ".zip", ".exe"} and size < 1024 * 1024:
        raise RuntimeError(f"binary is unexpectedly small: {path.name} ({size} bytes)")
    if suffix == ".zip":
        if not zipfile.is_zipfile(path):
            raise RuntimeError(f"invalid ZIP: {path.name}")
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad:
                raise RuntimeError(f"corrupt ZIP member in {path.name}: {bad}")
    elif suffix == ".exe" and magic[:2] != b"MZ":
        raise RuntimeError(f"invalid PE executable: {path.name}")
    elif suffix == ".pdf" and magic[:4] != b"%PDF":
        raise RuntimeError(f"invalid PDF: {path.name}")


def repo_name() -> str:
    value = os.environ.get("GITHUB_REPOSITORY", "LOVECHEN/dashcamviewer-release")
    if "/" not in value:
        raise RuntimeError("GITHUB_REPOSITORY is invalid")
    return value


def github_json(endpoint: str) -> object | None:
    result = run(
        ("gh", "api", endpoint),
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def release_info(tag: str) -> dict | None:
    payload = github_json(f"repos/{repo_name()}/releases/tags/{tag}")
    return payload if isinstance(payload, dict) else None


def release_assets(release: dict | None) -> dict[str, dict]:
    if not release:
        return {}
    return {
        str(asset["name"]): asset
        for asset in release.get("assets", [])
        if isinstance(asset, dict) and asset.get("name")
    }


def asset_checksums(assets: dict[str, dict]) -> dict[str, str]:
    checksums = {}
    for name, asset in assets.items():
        if name == "SHA256SUMS.txt":
            continue
        digest = str(asset.get("digest") or "")
        if digest.startswith("sha256:"):
            checksums[name] = digest.removeprefix("sha256:")
    return checksums


def parse_checksums(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
        if match:
            result[match.group(2)] = match.group(1).lower()
    return result


def download_existing_checksums(tag: str, directory: Path) -> dict[str, str]:
    target = directory / "existing-checksums"
    target.mkdir()
    result = run(
        (
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            repo_name(),
            "--pattern",
            "SHA256SUMS.txt",
            "--dir",
            str(target),
        ),
        check=False,
        capture=True,
    )
    path = target / "SHA256SUMS.txt"
    if result.returncode == 0 and path.exists():
        return parse_checksums(path.read_text(encoding="utf-8"))
    return {}


def write_checksums(path: Path, checksums: dict[str, str]) -> None:
    lines = [f"{digest}  {name}" for name, digest in sorted(checksums.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def upload(tag: str, paths: list[Path], *, clobber: bool = False) -> None:
    if not paths:
        return
    args = [
        "gh",
        "release",
        "upload",
        tag,
        "--repo",
        repo_name(),
    ]
    if clobber:
        args.append("--clobber")
    args.extend(str(path) for path in paths)
    for attempt in range(1, 6):
        result = run(args, check=False)
        if result.returncode == 0:
            return
        if attempt == 5:
            raise RuntimeError(f"release upload failed after {attempt} attempts: {tag}")
        log(f"  upload retry {attempt}/5")
        time.sleep(20)


def version_candidates(version: str) -> list[tuple[str, str]]:
    names = (
        f"DashcamViewerWin_v{version}.zip",
        f"Dashcam_Viewer_v{version}.dmg",
        f"Dashcam_Viewer_v{version}_AppleCPU.dmg",
    )
    return [(name, f"{OFFICIAL_BASE}/{name}") for name in names]


def available_version_assets(version: str) -> list[tuple[str, str]]:
    candidates = version_candidates(version)
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        statuses = list(pool.map(lambda item: probe(item[1]), candidates))
    return [item for item, exists in zip(candidates, statuses, strict=True) if exists]


def extract_executables(zip_path: Path, destination: Path) -> list[Path]:
    extracted = []
    with zipfile.ZipFile(zip_path) as archive:
        members = [item for item in archive.infolist() if not item.is_dir() and item.filename.lower().endswith(".exe")]
        for member in members:
            name = Path(member.filename).name
            if not name:
                continue
            target = destination / name
            if target.exists():
                raise RuntimeError(f"duplicate EXE name in {zip_path.name}: {name}")
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, 8 * 1024 * 1024)
            verify_file(target)
            extracted.append(target)
    if not extracted:
        raise RuntimeError(f"official Windows ZIP contains no EXE: {zip_path.name}")
    return extracted


class ChangelogParser(HTMLParser):
    """Convert the useful text blocks in a WordPress release post to Markdown."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.current: list[str] = []
        self.current_tag = ""
        self.strong_seen = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "li"}:
            self.current_tag = tag
            self.current = []
            self.strong_seen = False
        elif tag == "strong" and self.current_tag:
            self.strong_seen = True
        elif tag == "br" and self.current_tag:
            self.current.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag != self.current_tag:
            return
        text = re.sub(r"\s+", " ", "".join(self.current)).strip()
        if text:
            if self.current_tag == "li":
                self.blocks.append(f"- {text}")
            elif self.strong_seen and text.endswith(":"):
                self.blocks.append(f"### {text[:-1]}")
            elif not text.startswith("Dashcam Viewer and Dashcam Viewer PRO are"):
                self.blocks.append(text)
        self.current_tag = ""
        self.current = []
        self.strong_seen = False

    def handle_data(self, data: str) -> None:
        if self.current_tag:
            self.current.append(data)

    def markdown(self) -> str:
        return "\n\n".join(self.blocks).strip()


def discover_release_notes() -> dict[str, dict[str, str]]:
    notes: dict[str, dict[str, str]] = {}
    page_number = 1
    while True:
        endpoint = (
            f"{SITE_URL}/wp-json/wp/v2/posts"
            f"?per_page=100&page={page_number}&_fields=date,link,title,content"
        )
        payload = json.loads(curl_text(endpoint))
        if not isinstance(payload, list):
            raise RuntimeError("unexpected WordPress posts response")
        for post in payload:
            if not isinstance(post, dict):
                continue
            content = str((post.get("content") or {}).get("rendered") or "")
            title = html.unescape(str((post.get("title") or {}).get("rendered") or ""))
            plain = html.unescape(re.sub(r"<[^>]+>", " ", content))
            match = re.search(r"Dashcam Viewer\s+v(\d+(?:\.\d+){2,})", plain, re.I)
            if not match:
                match = re.search(r"Dashcam Viewer\s+v(\d+(?:\.\d+){2,})", title, re.I)
            if not match:
                continue
            parser = ChangelogParser()
            parser.feed(content)
            markdown = parser.markdown()
            if not markdown:
                continue
            notes[match.group(1)] = {
                "date": str(post.get("date") or "")[:10],
                "link": str(post.get("link") or ""),
                "markdown": markdown,
            }
        if len(payload) < 100:
            break
        page_number += 1
    log(f"official release notes discovered: {len(notes)}")
    return notes


def build_version_notes(
    names: list[str],
    checksums: dict[str, str],
    official_note: dict[str, str] | None,
) -> str:
    rows = []
    for name in sorted(names):
        if name.endswith("_AppleCPU.dmg"):
            kind = "macOS Apple Silicon DMG"
        elif name.endswith(".dmg"):
            kind = "macOS Intel DMG"
        elif name.endswith(".zip"):
            kind = "Windows ZIP"
        elif name.endswith(".exe"):
            kind = "Windows installer EXE (extracted byte-for-byte from official ZIP)"
        else:
            kind = "Official asset"
        rows.append(f"| `{name}` | {kind} |")
    checksum_lines = "\n".join(
        f"{checksums[name]}  {name}" for name in sorted(names) if name in checksums
    )
    if official_note:
        official = f"""**Released:** {official_note['date']}

{official_note['markdown']}
"""
    else:
        official = ""
    prefix = f"{official.rstrip()}\n\n" if official else ""
    return f"""{prefix}## Files

| File | Description |
|---|---|
{chr(10).join(rows)}

**SHA-256**

```text
{checksum_lines}
```
"""


def create_release(tag: str, title: str, notes: Path, *, latest: bool = False) -> None:
    args = (
        "gh",
        "release",
        "create",
        tag,
        "--repo",
        repo_name(),
        "--title",
        title,
        "--notes-file",
        str(notes),
        "--latest" if latest else "--latest=false",
    )
    run(args)


def edit_release(tag: str, title: str, notes: Path, *, latest: bool = False) -> None:
    args = [
        "gh",
        "release",
        "edit",
        tag,
        "--repo",
        repo_name(),
        "--title",
        title,
        "--notes-file",
        str(notes),
    ]
    if latest:
        args.append("--latest")
    run(args)


def sync_version(
    version: str,
    current_version: str,
    release_notes: dict[str, dict[str, str]],
) -> bool:
    tag = f"v{version}"
    existing_release = release_info(tag)
    existing_assets = release_assets(existing_release)
    available = available_version_assets(version)
    if not available:
        log(f"⏭ {tag}: official CDN has no matching assets")
        return False

    log(f"═══ {tag}: {len(available)} official direct assets ═══")
    direct_names = {name for name, _ in available}
    has_windows_zip = any(name.lower().endswith(".zip") and "win" in name.lower() for name in direct_names)
    has_exe = any(name.lower().endswith(".exe") for name in existing_assets)
    missing_direct = direct_names - set(existing_assets)
    needs_windows_extract = has_windows_zip and not has_exe
    if existing_release and not missing_direct and not needs_windows_extract and "SHA256SUMS.txt" in existing_assets:
        with tempfile.TemporaryDirectory(prefix=f"dcv-notes-{version}-") as temporary:
            directory = Path(temporary)
            final_names = sorted(set(existing_assets) - {"SHA256SUMS.txt"})
            checksums = asset_checksums(existing_assets)
            checksums.update(download_existing_checksums(tag, directory))
            notes = directory / "notes.md"
            notes.write_text(
                build_version_notes(final_names, checksums, release_notes.get(version)),
                encoding="utf-8",
            )
            edit_release(
                tag,
                f"Dashcam Viewer v{version}",
                notes,
                latest=version == current_version,
            )
        log(f"✓ {tag}: complete")
        return False

    created = existing_release is None
    with tempfile.TemporaryDirectory(prefix=f"dcv-{version}-") as temporary:
        directory = Path(temporary)
        local_assets: list[Path] = []
        for name, url in available:
            if name not in missing_direct and not (needs_windows_extract and name.lower().endswith(".zip")):
                continue
            target = directory / name
            download(url, target)
            verify_file(target)
            if name in missing_direct:
                local_assets.append(target)

            if name.lower().endswith(".zip") and "win" in name.lower() and needs_windows_extract:
                for executable in extract_executables(target, directory):
                    if executable.name not in existing_assets:
                        local_assets.append(executable)

        checksums = asset_checksums(existing_assets)
        checksums.update(download_existing_checksums(tag, directory) if existing_release else {})
        for path in local_assets:
            checksums[path.name] = sha256_file(path)

        final_names = sorted((set(existing_assets) - {"SHA256SUMS.txt"}) | {path.name for path in local_assets})
        missing_hashes = [name for name in final_names if name not in checksums]
        if missing_hashes:
            raise RuntimeError(f"missing checksums for {tag}: {', '.join(missing_hashes)}")

        checksum_file = directory / "SHA256SUMS.txt"
        write_checksums(checksum_file, {name: checksums[name] for name in final_names})
        notes = directory / "notes.md"
        notes.write_text(
            build_version_notes(final_names, checksums, release_notes.get(version)),
            encoding="utf-8",
        )

        if created:
            create_release(tag, f"Dashcam Viewer v{version}", notes, latest=version == current_version)
        upload(tag, local_assets)
        upload(tag, [checksum_file], clobber=True)
        edit_release(tag, f"Dashcam Viewer v{version}", notes, latest=version == current_version)

    log(f"✅ {tag}: {'created' if created else 'updated'}")
    return created


def discover_current() -> tuple[str, str]:
    page = curl_text(FREE_TRIAL_URL)
    page = html.unescape(page)
    match = re.search(r"DashcamViewerWin_v(\d+(?:\.\d+)+)\.zip", page)
    if not match:
        raise RuntimeError("cannot determine current Dashcam Viewer version")
    return match.group(1), page


def safe_asset_name(url: str) -> str:
    name = urllib.parse.unquote(Path(urllib.parse.urlsplit(url).path).name)
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", name).strip("_")


def discover_support_urls(free_trial_page: str) -> list[tuple[str, str]]:
    hrefs = [html.unescape(value) for value in re.findall(r'href=["\']([^"\']+)["\']', free_trial_page)]
    wanted = []
    patterns = (
        "Dashcam%20Viewer%20Users%20Manual.pdf",
        "DashcamViewer_EULA.html",
        "DashcamViewer_Uninstall.html",
        "win_installation_notes/install_win.html",
        "K-Lite_Codec_Pack_",
    )
    for url in hrefs:
        if url.startswith("http") and any(pattern in url for pattern in patterns):
            wanted.append(url)
    wanted.extend(
        (
            f"{OFFICIAL_BASE}/history.pdf",
            GPS_CONVERTER_URL,
        )
    )
    result = []
    seen = set()
    for url in wanted:
        name = safe_asset_name(url)
        if not name or name in seen:
            continue
        seen.add(name)
        result.append((name, url))
    return result


def build_support_notes(names: list[str], checksums: dict[str, str]) -> str:
    rows = "\n".join(f"| `{name}` | `{checksums[name]}` |" for name in sorted(names))
    return f"""## Dashcam Viewer support files

Current support files linked by the official Dashcam Viewer website.

This Release includes the user manual, full version history, Windows installation/EULA/uninstall documents, the required K-Lite Codec Pack, and the Visiondrive converter when available.

| File | SHA-256 |
|---|---|
{rows}
"""


def sync_support(free_trial_page: str) -> None:
    tag = "support-current"
    candidates = discover_support_urls(free_trial_page)
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
        statuses = list(pool.map(lambda item: probe(item[1]), candidates))
    available = [item for item, exists in zip(candidates, statuses, strict=True) if exists]
    if not available:
        raise RuntimeError("no support files are available")

    existing_release = release_info(tag)
    existing_assets = release_assets(existing_release)
    with tempfile.TemporaryDirectory(prefix="dcv-support-") as temporary:
        directory = Path(temporary)
        files = []
        checksums = {}
        for name, url in available:
            target = directory / name
            download(url, target)
            verify_file(target)
            files.append(target)
            checksums[name] = sha256_file(target)

        old_checksums = asset_checksums(existing_assets)
        old_checksums.update(download_existing_checksums(tag, directory) if existing_release else {})
        notes = directory / "notes.md"
        notes.write_text(build_support_notes(list(checksums), checksums), encoding="utf-8")
        if existing_release and checksums == {name: old_checksums.get(name) for name in checksums}:
            edit_release(tag, "Dashcam Viewer support files", notes, latest=False)
            log("✓ support-current: unchanged")
            return

        checksum_file = directory / "SHA256SUMS.txt"
        write_checksums(checksum_file, checksums)
        if not existing_release:
            create_release(tag, "Dashcam Viewer support files", notes, latest=False)
        upload(tag, files + [checksum_file], clobber=True)
        edit_release(tag, "Dashcam Viewer support files", notes, latest=False)
        log("✅ support-current: synchronized")


def historical_versions() -> list[str]:
    values = []
    for line in (ROOT / "versions.txt").read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            values.append(value)
    return values


def probe_history() -> None:
    versions = historical_versions()
    log("version\twindows_zip\tmac_intel\tmac_apple_silicon")
    for version in versions:
        available = {name for name, _ in available_version_assets(version)}
        names = [name for name, _ in version_candidates(version)]
        states = ["yes" if name in available else "no" for name in names]
        log(f"{version}\t" + "\t".join(states))


def backfill(
    current_version: str,
    max_versions: int,
    release_notes: dict[str, dict[str, str]],
) -> None:
    created = 0
    for version in historical_versions():
        if version == current_version or release_info(f"v{version}"):
            continue
        if sync_version(version, current_version, release_notes):
            created += 1
            if created >= max_versions:
                break
    log(f"backfill: created {created} historical releases")


def refresh_release_notes(
    current_version: str,
    release_notes: dict[str, dict[str, str]],
) -> None:
    result = run(
        (
            "gh",
            "release",
            "list",
            "--repo",
            repo_name(),
            "--limit",
            "1000",
            "--json",
            "tagName",
        ),
        capture=True,
    )
    releases = json.loads(result.stdout)
    updated = 0
    for record in releases:
        tag = str(record.get("tagName") or "")
        match = re.fullmatch(r"v(\d+(?:\.\d+)+)", tag)
        if not match:
            continue
        version = match.group(1)
        info = release_info(tag)
        assets = release_assets(info)
        names = sorted(set(assets) - {"SHA256SUMS.txt"})
        with tempfile.TemporaryDirectory(prefix=f"dcv-notes-{version}-") as temporary:
            directory = Path(temporary)
            checksums = asset_checksums(assets)
            checksums.update(download_existing_checksums(tag, directory))
            notes = directory / "notes.md"
            notes.write_text(
                build_version_notes(names, checksums, release_notes.get(version)),
                encoding="utf-8",
            )
            edit_release(
                tag,
                f"Dashcam Viewer v{version}",
                notes,
                latest=version == current_version,
            )
        updated += 1
    log(f"release notes refreshed: {updated}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("all", "current", "backfill", "support", "notes", "probe"),
    )
    parser.add_argument("--max-versions", type=int, default=4)
    args = parser.parse_args()
    if args.max_versions < 1:
        parser.error("--max-versions must be positive")
    return args


def main() -> int:
    args = parse_args()
    current_version, free_trial_page = discover_current()
    log(f"current version: {current_version}")
    if args.mode == "probe":
        probe_history()
        return 0
    release_notes = discover_release_notes() if args.mode != "support" else {}
    if args.mode in {"all", "current"}:
        sync_version(current_version, current_version, release_notes)
    if args.mode in {"all", "support"}:
        sync_support(free_trial_page)
    if args.mode in {"all", "backfill"}:
        backfill(current_version, args.max_versions, release_notes)
    if args.mode == "notes":
        refresh_release_notes(current_version, release_notes)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        log(f"command failed ({error.returncode}): {' '.join(error.cmd)}")
        raise SystemExit(error.returncode) from error
    except Exception as error:
        log(f"fatal: {type(error).__name__}: {error}")
        raise SystemExit(1) from error
