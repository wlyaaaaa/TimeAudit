from pathlib import Path


ROOT = Path(__file__).resolve().parent
AHK_SCRIPT = ROOT / "TimeAudit.ahk"


def test_ahk_timestamp_uses_runtime_utc_offset():
    source = AHK_SCRIPT.read_text(encoding="utf-8-sig")

    assert '. "+08"' not in source
    assert "GetUtcOffsetSuffix()" in source
    assert "A_NowUTC" in source


if __name__ == "__main__":
    test_ahk_timestamp_uses_runtime_utc_offset()
