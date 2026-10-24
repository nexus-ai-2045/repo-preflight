"""references/gitleaks-pins.json の版とハッシュで gitleaks を入れる（CI 用）。

preflight-gate.yml と同じ版・同じ配布物のハッシュを使う。配布物と取り出した本体の
両方をハッシュで確かめ、どちらかが合わなければ何も置かずに止める。
Linux・macOS・Windows の CI で同じ手順にするため、シェルではなく Python で書く。
"""

import argparse
import hashlib
import io
import json
import os
import platform
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

PINS = Path(__file__).resolve().parent.parent / "references" / "gitleaks-pins.json"
URL = "https://github.com/gitleaks/gitleaks/releases/download/v{v}/gitleaks_{v}_{key}.{ext}"


def platform_key() -> str:
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}
    if machine not in arch:
        raise SystemExit(f"失敗: この CPU（{machine}）の固定版がありません")
    if sys.platform.startswith("linux"):
        system = "linux"
    elif sys.platform == "darwin":
        system = "darwin"
    elif sys.platform == "win32":
        system = "windows"
    else:
        raise SystemExit(f"失敗: この OS（{sys.platform}）の固定版がありません")
    return f"{system}_{arch[machine]}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract(archive: bytes, windows: bool) -> bytes:
    name = "gitleaks.exe" if windows else "gitleaks"
    if windows:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            return zf.read(name)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        member = tf.getmember(name)
        if not member.isfile():
            raise SystemExit("失敗: 配布物の gitleaks が通常のファイルではありません")
        stream = tf.extractfile(member)
        assert stream is not None
        return stream.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", required=True, help="gitleaks を置くフォルダ")
    parser.add_argument(
        "--github-path",
        action="store_true",
        help="置いたフォルダを GITHUB_PATH に足す（後の段の PATH に入る）",
    )
    args = parser.parse_args()

    pins = json.loads(PINS.read_text(encoding="utf-8"))
    key = platform_key()
    windows = key.startswith("windows_")
    version = pins["version"]
    archive_pin = pins["archive_sha256"][key]
    binary_pin = pins["binary_sha256"][key]
    url = URL.format(v=version, key=key, ext="zip" if windows else "tar.gz")

    with urllib.request.urlopen(url, timeout=120) as response:
        archive = response.read()
    if sha256(archive) != archive_pin:
        raise SystemExit("失敗: gitleaks の配布物のハッシュが固定値と一致しません")
    binary = extract(archive, windows)
    if sha256(binary) != binary_pin:
        raise SystemExit("失敗: gitleaks 本体のハッシュが固定値と一致しません")

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / ("gitleaks.exe" if windows else "gitleaks")
    target.write_bytes(binary)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if args.github_path:
        github_path = os.environ.get("GITHUB_PATH")
        if not github_path:
            raise SystemExit("失敗: GITHUB_PATH がありません（CI の外では使いません）")
        with open(github_path, "a", encoding="utf-8") as stream:
            stream.write(f"{dest.resolve()}\n")
    print(f"gitleaks {version}（{key}）を確かめて置きました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
