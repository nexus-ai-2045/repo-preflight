"""架空のhome pathをテスト時に組み立てる。実環境の値は読まない。"""


def synthetic_home_path(kind, relative, *, separator="/", prefix=""):
    roots = {
        "windows": ("C:", "Users"),
        "macos": ("", "Users"),
        "linux": ("", "home"),
    }
    return prefix + separator.join((*roots[kind], relative))
