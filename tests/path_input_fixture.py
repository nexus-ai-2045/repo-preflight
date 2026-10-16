"""架空の負例データ専用factory。環境・実HOME・filesystemを参照しない。"""

import json
from pathlib import PurePosixPath, PureWindowsPath


def fixture_root(flavour: str, *, forward: bool = False) -> str:
    """検査対象の架空home rootを、username無しで作る。"""
    if flavour == "windows":
        path = PureWindowsPath("C:/", "Users")
        return (path.as_posix() if forward else str(path)) + ("/" if forward else "\\")
    folder = {"macos": "Users", "linux": "home"}[flavour]
    return str(PurePosixPath("/", folder)) + "/"


def fixture_path(
    flavour: str, username: str, *parts: str, forward: bool = False
) -> str:
    """明示された架空usernameと部品からテスト入力を作る。"""
    base = fixture_root(flavour, forward=forward)
    separator = "\\" if flavour == "windows" and not forward else "/"
    return base + separator.join((username, *parts))


def split_path_source(path: str) -> str:
    """静的な加算を点検するテスト用sourceを、生成済み入力から作る。"""
    return "PATH = " + json.dumps(path[:1]) + " + " + json.dumps(path[1:]) + "\n"


def fixture_relative(*parts: str) -> str:
    """相対pathの負例対照データを作り、明示dot prefixも保存する。"""
    path = str(PurePosixPath(*parts))
    return "./" + path if parts and parts[0] == "." else path
