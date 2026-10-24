"""The reviewed official source artifacts retain exact raw-byte binding."""

import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.mark.skipif(
    sys.version_info < (3, 11), reason="Pinned fontTools requires 3.11+"
)
@pytest.mark.parametrize("tamper", [False, True])
def test_font_reader_windows_sources_preserve_exact_pin(tmp_path, monkeypatch, tamper):
    import fontTools.ttLib.ttFont as tt_source
    import fontTools.ttLib.sfnt as sfnt_source

    script = Path(__file__).resolve().parents[1] / "scripts/media_identity.py"
    spec = importlib.util.spec_from_file_location("platform_font_media", script)
    media = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(media)
    for index, module in enumerate((tt_source, sfnt_source)):
        source = Path(module.__file__).read_bytes().replace(b"\r\n", b"\n")
        artifact = tmp_path / f"reader-{index}.py"
        artifact.write_bytes(
            source.replace(b"\n", b"\r\n") + (b"# changed\r\n" if tamper else b"")
        )
        monkeypatch.setattr(module, "__file__", str(artifact))
    if tamper:
        with pytest.raises(media.MediaError, match="dependency_unavailable"):
            media._verify_font_reader_sources()
    else:
        media._verify_font_reader_sources()
