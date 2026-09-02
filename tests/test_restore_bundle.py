import io
import tarfile

import pytest
import zstandard

from backend.restore_bundle import (
    BundleFormat,
    BundleItem,
    ManifestBuilder,
    build_extract_command,
    build_verify_command,
    build_zst_probe_blob,
    probe_tar_zst_support,
    select_bundle_format,
)

# --- BundleItem -------------------------------------------------------


def test_bundle_item_defaults_to_leaf():
    item = BundleItem(filepath="abc==", name="hosts")
    assert item.leaf is True


def test_bundle_item_directory():
    item = BundleItem(filepath="abc==", name="etc", leaf=False)
    assert item.leaf is False


# --- ManifestBuilder ----------------------------------------------------


def test_manifest_builder_empty():
    m = ManifestBuilder()
    assert len(m) == 0
    assert m.render() == ""


def test_manifest_builder_renders_sha256sum_compatible_format():
    m = ManifestBuilder()
    m.add("etc/hosts", "abc123")
    m.add("etc/passwd", "def456")
    assert len(m) == 2
    assert m.render() == "abc123  etc/hosts\ndef456  etc/passwd\n"


# --- build_zst_probe_blob -------------------------------------------------


def test_build_zst_probe_blob_is_a_real_extractable_tar_zst():
    # Round-trips through the real zstandard/tarfile libraries, proving
    # the blob probe_tar_zst_support() sends to a guest is genuinely a
    # valid .tar.zst containing exactly what the probe expects back.
    blob = build_zst_probe_blob()
    raw_tar = zstandard.ZstdDecompressor().decompress(blob)
    with tarfile.open(fileobj=io.BytesIO(raw_tar)) as tf:
        member = tf.getmember("probe")
        content = tf.extractfile(member).read()
    assert content == b"ok"


def test_build_zst_probe_blob_is_deterministic_content():
    # Not byte-identical necessarily (compressor framing can vary), but
    # decompresses to the same thing every time.
    a = zstandard.ZstdDecompressor().decompress(build_zst_probe_blob())
    b = zstandard.ZstdDecompressor().decompress(build_zst_probe_blob())
    assert a == b


# --- probe_tar_zst_support ------------------------------------------------


async def test_probe_tar_zst_support_true_when_extraction_succeeds():
    async def fake_write(path, content):
        pass

    async def fake_exec(argv):
        assert argv[0] == "tar"
        return 0, "ok", ""

    assert await probe_tar_zst_support(fake_write, fake_exec, "/tmp/probe.tar.zst") is True


async def test_probe_tar_zst_support_false_on_nonzero_exit():
    async def fake_write(path, content):
        pass

    async def fake_exec(argv):
        return 1, "", "tar: unrecognized archive format"

    assert await probe_tar_zst_support(fake_write, fake_exec, "/tmp/probe.tar.zst") is False


async def test_probe_tar_zst_support_false_on_unexpected_output():
    # Exit code 0 but wrong content - shouldn't happen with a real tar,
    # but the probe shouldn't trust exit code alone (same lesson as
    # copy /b's unreliable exit code, docs/plan.md §7.5).
    async def fake_write(path, content):
        pass

    async def fake_exec(argv):
        return 0, "not-ok", ""

    assert await probe_tar_zst_support(fake_write, fake_exec, "/tmp/probe.tar.zst") is False


async def test_probe_tar_zst_support_false_on_any_exception_not_fatal():
    async def fail_write(path, content):
        raise RuntimeError("guest unreachable")

    async def fail_if_called(argv):
        raise AssertionError("should not exec if the write already failed")

    assert await probe_tar_zst_support(fail_write, fail_if_called, "/tmp/probe.tar.zst") is False


# --- select_bundle_format --------------------------------------------------


def test_select_bundle_format_prefers_native_tarzst_when_capable():
    assert select_bundle_format("linux", zst_capable=True) == BundleFormat.TAR_ZST
    assert select_bundle_format("windows", zst_capable=True) == BundleFormat.TAR_ZST


def test_select_bundle_format_falls_back_to_zip_on_windows():
    assert select_bundle_format("windows", zst_capable=False) == BundleFormat.ZIP


def test_select_bundle_format_falls_back_to_targz_on_posix():
    assert select_bundle_format("linux", zst_capable=False) == BundleFormat.TAR_GZ
    assert select_bundle_format("bsd", zst_capable=False) == BundleFormat.TAR_GZ
    assert select_bundle_format(None, zst_capable=False) == BundleFormat.TAR_GZ


# --- build_extract_command --------------------------------------------------


def test_build_extract_command_tar_formats():
    for fmt in (BundleFormat.TAR_ZST, BundleFormat.TAR_GZ):
        argv = build_extract_command(fmt, "/tmp/bundle.tar", "/home/user/restore", "linux")
        assert argv == ["tar", "-xf", "/tmp/bundle.tar", "-C", "/home/user/restore"]


def test_build_extract_command_zip_uses_expand_archive():
    argv = build_extract_command(BundleFormat.ZIP, "C:\\Temp\\bundle.zip", "C:\\restore", "windows")
    assert argv[0] == "powershell"
    script = argv[-1]
    assert "Expand-Archive" in script
    assert "C:\\Temp\\bundle.zip" in script
    assert "C:\\restore" in script


def test_build_extract_command_unknown_format_raises():
    with pytest.raises(ValueError):
        build_extract_command("bogus", "/tmp/x", "/tmp/y", "linux")


# --- build_verify_command --------------------------------------------------


def test_build_verify_command_linux_uses_sha256sum_dash_c():
    argv = build_verify_command("/home/user/restore/.pve-flr-manifest.sha256", "/home/user/restore", "linux")
    assert argv[:2] == ["sh", "-c"]
    assert "sha256sum -c" in argv[2]
    assert "/home/user/restore" in argv[2]


def test_build_verify_command_windows_uses_get_filehash():
    argv = build_verify_command("C:\\restore\\.pve-flr-manifest.sha256", "C:\\restore", "windows")
    assert argv[0] == "powershell"
    script = argv[-1]
    assert "Get-FileHash" in script
    assert "ALL-OK" in script
