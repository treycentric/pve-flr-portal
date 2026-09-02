import hashlib
import io
import tarfile
import zipfile

import pytest
import zstandard

from backend import pve_client, restore_bundle
from backend.restore_bundle import (
    MANIFEST_NAME,
    BundleFormat,
    BundleItem,
    ManifestBuilder,
    build_bundle,
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


# --- build_bundle (real-library round trip, no live guest) ----------------


class _FakeBundleResponse:
    def __init__(self, content: bytes):
        self._content = content

    async def aiter_bytes(self, chunk_size: int):
        for start in range(0, len(self._content), chunk_size):
            yield self._content[start : start + chunk_size]
        if not self._content:
            yield b""

    async def aclose(self) -> None:
        pass


class _FakeBundleClient:
    async def aclose(self) -> None:
        pass


def _fake_directory_zip(files: dict[str, bytes], prefix: str = "") -> bytes:
    """A real, valid zip - what PVE's own default directory encoding
    would hand back (docs/plan.md §7.7's correction: no tar=1 needed).
    `prefix`, when given, roots every entry under it - PVE's own zip for
    a directory already roots every entry under the directory's own name
    (confirmed live 2026-09-02 by a doubled `Downloads/Downloads/` when
    this app's own code re-prefixed on top of that), so callers building
    a fixture for a directory selection should pass the item's own name
    here rather than leaving entries unprefixed."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, content in files.items():
            zf.writestr(f"{prefix}{name}" if prefix else name, content)
    return buf.getvalue()


def _patch_bundle_download(monkeypatch, responses: dict[str, bytes]):
    """responses maps filepath -> raw bytes PVE would return for it."""

    async def fake_open_download(session, volume, filepath, tar=False):
        return _FakeBundleClient(), _FakeBundleResponse(responses[filepath])

    monkeypatch.setattr(pve_client, "open_download", fake_open_download)


async def test_build_bundle_zip_contains_every_item_plus_manifest(session_data, monkeypatch, tmp_path):
    hosts = b"127.0.0.1 localhost\n"
    passwd = b"root:x:0:0::/root:/bin/bash\n"
    shadow = b"root:!:19000:0:99999:7:::\n"
    _patch_bundle_download(
        monkeypatch,
        {
            "L2V0Yy9ob3N0cw==": hosts,
            "ZXRj": _fake_directory_zip({"passwd": passwd, "shadow": shadow}, prefix="etc/"),
        },
    )
    items = [
        BundleItem(filepath="L2V0Yy9ob3N0cw==", name="hosts", leaf=True),
        BundleItem(filepath="ZXRj", name="etc", leaf=False),
    ]

    output_path, fmt, manifest, tmp_dir_ctx = await build_bundle(
        session_data, "pbs:backup/vm/133/2026-09-01", items, guest_os_family="linux", zst_capable=False
    )
    try:
        assert fmt == BundleFormat.TAR_GZ  # not zst-capable, linux -> targz fallback
        with tarfile.open(output_path, mode="r:gz") as tf:
            names = tf.getnames()
            assert "hosts" in names
            assert "etc/passwd" in names
            assert "etc/shadow" in names
            assert MANIFEST_NAME in names
            assert tf.extractfile("hosts").read() == hosts
            assert tf.extractfile("etc/passwd").read() == passwd
            manifest_text = tf.extractfile(MANIFEST_NAME).read().decode()

        assert len(manifest) == 3  # hosts, etc/passwd, etc/shadow - not the manifest itself
        assert f"{hashlib.sha256(hosts).hexdigest()}  hosts" in manifest_text
        assert f"{hashlib.sha256(passwd).hexdigest()}  etc/passwd" in manifest_text
        assert f"{hashlib.sha256(shadow).hexdigest()}  etc/shadow" in manifest_text
    finally:
        tmp_dir_ctx.cleanup()


async def test_build_bundle_directory_entries_are_not_double_prefixed(session_data, monkeypatch, tmp_path):
    # Confirmed live 2026-09-02: a restored "Downloads" directory landed
    # as "Downloads/Downloads/..." in the destination - PVE's own zip
    # for a directory selection already roots every entry under the
    # directory's own name, and this code used to prepend item.name on
    # top of that again. Locks in that info.filename is trusted as-is.
    content = b"family photo bytes"
    dir_zip = _fake_directory_zip({"photo.jpg": content}, prefix="Downloads/")
    _patch_bundle_download(monkeypatch, {"ZG93bmxvYWRz==": dir_zip})
    items = [BundleItem(filepath="ZG93bmxvYWRz==", name="Downloads", leaf=False)]

    output_path, _fmt, manifest, tmp_dir_ctx = await build_bundle(
        session_data, "pbs:backup/vm/133/2026-09-01", items, guest_os_family="linux", zst_capable=False
    )
    try:
        with tarfile.open(output_path, mode="r:gz") as tf:
            names = tf.getnames()
            assert "Downloads/photo.jpg" in names
            assert "Downloads/Downloads/photo.jpg" not in names
        assert manifest.render() == f"{hashlib.sha256(content).hexdigest()}  Downloads/photo.jpg\n"
    finally:
        tmp_dir_ctx.cleanup()


async def test_build_bundle_deletes_each_item_temp_file_as_it_is_consumed(session_data, monkeypatch, tmp_path):
    # Confirmed live 2026-09-01: downloading every selected item to a
    # local temp file before building anything ran a real LXC
    # container's rootfs out of space ("[Errno 28] No space left on
    # device") on a multi-item selection. This locks in the fix - at
    # most one item's temp file (plus the growing output bundle) should
    # ever exist in the working directory at once, not every item's.
    import os

    a = b"a" * 1000
    b = b"b" * 1000
    c = b"c" * 1000
    _patch_bundle_download(monkeypatch, {"a==": a, "b==": b, "c==": c})
    items = [
        BundleItem(filepath="a==", name="a.txt", leaf=True),
        BundleItem(filepath="b==", name="b.txt", leaf=True),
        BundleItem(filepath="c==", name="c.txt", leaf=True),
    ]

    seen_item_file_counts = []
    tmp_dir_holder = {}

    real_add = restore_bundle._add_item_to_bundle_writer

    def _spying_add(writer, item, local_path, manifest):
        # Snapshot how many "item-*" temp files exist in the working
        # directory at the moment each item is actually being added -
        # should never be more than the one currently being processed.
        tmp_dir_holder["dir"] = local_path.parent
        count = sum(1 for p in local_path.parent.iterdir() if p.name.startswith("item-"))
        seen_item_file_counts.append(count)
        return real_add(writer, item, local_path, manifest)

    monkeypatch.setattr(restore_bundle, "_add_item_to_bundle_writer", _spying_add)

    output_path, _fmt, _manifest, tmp_dir_ctx = await build_bundle(
        session_data, "pbs:backup/vm/133/2026-09-01", items, guest_os_family="linux", zst_capable=False
    )
    try:
        assert seen_item_file_counts == [1, 1, 1]  # never more than the one currently being added
        # And nothing lingers afterward either - just the finished bundle.
        remaining = os.listdir(tmp_dir_holder["dir"])
        assert remaining == [output_path.name]
    finally:
        tmp_dir_ctx.cleanup()


async def test_build_bundle_zip_format_when_windows_and_not_zst_capable(session_data, monkeypatch, tmp_path):
    content = b"some file content"
    _patch_bundle_download(monkeypatch, {"abc==": content})
    items = [BundleItem(filepath="abc==", name="notes.txt", leaf=True)]

    output_path, fmt, _manifest, tmp_dir_ctx = await build_bundle(
        session_data, "pbs:backup/vm/202/2026-09-01", items, guest_os_family="windows", zst_capable=False
    )
    try:
        assert fmt == BundleFormat.ZIP
        with zipfile.ZipFile(output_path) as zf:
            assert zf.read("notes.txt") == content
            manifest_text = zf.read(MANIFEST_NAME).decode()
        assert f"{hashlib.sha256(content).hexdigest()}  notes.txt" in manifest_text
    finally:
        tmp_dir_ctx.cleanup()


async def test_build_bundle_tarzst_when_capable(session_data, monkeypatch, tmp_path):
    content = b"a" * 5000
    _patch_bundle_download(monkeypatch, {"abc==": content})
    items = [BundleItem(filepath="abc==", name="bigfile.bin", leaf=True)]

    output_path, fmt, _manifest, tmp_dir_ctx = await build_bundle(
        session_data, "pbs:backup/vm/202/2026-09-01", items, guest_os_family="linux", zst_capable=True
    )
    try:
        assert fmt == BundleFormat.TAR_ZST
        # A streaming-compressed frame (zstandard's stream_writer, as
        # build_bundle() uses) doesn't record its total content size in
        # the frame header, so the one-shot decompress() API can't
        # handle it - stream_reader() is the correct way to decompress
        # this shape, matching what a real guest's `tar` would do too.
        with output_path.open("rb") as compressed:
            raw_tar = zstandard.ZstdDecompressor().stream_reader(compressed).read()
        with tarfile.open(fileobj=io.BytesIO(raw_tar)) as tf:
            assert tf.extractfile("bigfile.bin").read() == content
            assert MANIFEST_NAME in tf.getnames()
    finally:
        tmp_dir_ctx.cleanup()


async def test_build_bundle_manifest_omits_directory_entries_from_source_zip(session_data, monkeypatch, tmp_path):
    # A real zip from a nested directory selection often includes
    # explicit directory-marker entries (names ending in "/", zero
    # size) - these shouldn't end up as bogus manifest lines with no
    # real file behind them.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("mydir/sub/", b"")  # directory marker entry
        zf.writestr("mydir/sub/file.txt", b"hello")
    _patch_bundle_download(monkeypatch, {"dir==": buf.getvalue()})
    items = [BundleItem(filepath="dir==", name="mydir", leaf=False)]

    _output_path, _fmt, manifest, tmp_dir_ctx = await build_bundle(
        session_data, "pbs:backup/vm/202/2026-09-01", items, guest_os_family="linux", zst_capable=False
    )
    try:
        # Exactly one real entry - the directory-marker "sub/" itself
        # never becomes a bogus manifest line, but the real subdirectory
        # structure inside it is preserved correctly.
        assert len(manifest) == 1
        assert manifest.render() == f"{hashlib.sha256(b'hello').hexdigest()}  mydir/sub/file.txt\n"
    finally:
        tmp_dir_ctx.cleanup()
