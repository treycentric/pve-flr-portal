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
        self.headers = {}  # PVE not always sending Content-Length is the realistic default

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


def _fake_directory_tar(
    files: dict[str, bytes], prefix: str = "", uid: int = 0, gid: int = 0, mode: int = 0o644, mtime: int = 0
) -> bytes:
    """A real, valid tar - what a non-Windows guest's directory item now
    downloads via `tar=1` instead of the old default zip (issue #26,
    confirmed live 2026-09-28 - real per-member uid/gid/mode/mtime
    survives this path). Same no-re-prefixing convention as
    `_fake_directory_zip` - pass the item's own name as `prefix` for a
    directory selection."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in files.items():
            info = tarfile.TarInfo(name=f"{prefix}{name}" if prefix else name)
            info.size = len(content)
            info.uid = uid
            info.gid = gid
            info.mode = mode
            info.mtime = mtime
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _patch_bundle_download(monkeypatch, responses: dict):
    """responses maps filepath -> raw bytes PVE would return for it,
    used regardless of `tar` - or filepath -> {True: ..., False: ...}
    when a test needs to distinguish (issue #26: a leaf item's metadata
    probe always requests `tar=1` separately from its `tar=0` content
    download; a non-Windows directory item's content download IS
    `tar=1` now, not the old default `tar=0` zip)."""

    async def fake_open_download(session, volume, filepath, tar=False):
        entry = responses[filepath]
        content = entry[tar] if isinstance(entry, dict) else entry
        return _FakeBundleClient(), _FakeBundleResponse(content)

    monkeypatch.setattr(pve_client, "open_download", fake_open_download)


async def test_build_bundle_zip_contains_every_item_plus_manifest(session_data, monkeypatch, tmp_path):
    hosts = b"127.0.0.1 localhost\n"
    passwd = b"root:x:0:0::/root:/bin/bash\n"
    shadow = b"root:!:19000:0:99999:7:::\n"
    _patch_bundle_download(
        monkeypatch,
        {
            "L2V0Yy9ob3N0cw==": hosts,
            "ZXRj": _fake_directory_tar({"passwd": passwd, "shadow": shadow}, prefix="etc/"),
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


async def test_build_bundle_reports_item_progress_with_content_length(session_data, monkeypatch, tmp_path):
    # 2026-09-02: the build phase (download + add-to-bundle) had no
    # progress signal at all - confirmed live to look indistinguishable
    # from a hang on a large single-directory selection. Locks in that
    # on_item_progress fires with real byte counts, including the final
    # chunk reaching the item's full declared size.
    content = b"x" * (61440 + 100)  # spans more than one read

    async def fake_open_download(session, volume, filepath, tar=False):
        response = _FakeBundleResponse(content)
        response.headers = {"content-length": str(len(content))}
        return _FakeBundleClient(), response

    monkeypatch.setattr(pve_client, "open_download", fake_open_download)
    items = [BundleItem(filepath="abc==", name="big.bin", leaf=True)]

    calls = []

    def on_progress(item, downloaded, total):
        calls.append((item.name, downloaded, total))

    _output_path, _fmt, _manifest, tmp_dir_ctx = await build_bundle(
        session_data,
        "pbs:backup/vm/133/2026-09-01",
        items,
        guest_os_family="linux",
        zst_capable=False,
        on_item_progress=on_progress,
    )
    try:
        assert len(calls) >= 2  # more than one chunk given the content size
        assert all(name == "big.bin" and total == len(content) for name, _downloaded, total in calls)
        assert calls[-1][1] == len(content)  # the last call reports the full size downloaded
        # Monotonically increasing, never resets or double-counts.
        assert [c[1] for c in calls] == sorted(c[1] for c in calls)
    finally:
        tmp_dir_ctx.cleanup()


async def test_build_bundle_reports_item_progress_without_content_length(session_data, monkeypatch, tmp_path):
    # PVE doesn't always send Content-Length - the callback still fires
    # (so the caller can at least log periodically), just with total=None
    # rather than not firing at all.
    content = b"y" * 500
    _patch_bundle_download(monkeypatch, {"abc==": content})
    items = [BundleItem(filepath="abc==", name="small.bin", leaf=True)]

    calls = []

    def on_progress(item, downloaded, total):
        calls.append((item.name, downloaded, total))

    _output_path, _fmt, _manifest, tmp_dir_ctx = await build_bundle(
        session_data,
        "pbs:backup/vm/133/2026-09-01",
        items,
        guest_os_family="linux",
        zst_capable=False,
        on_item_progress=on_progress,
    )
    try:
        assert calls == [("small.bin", len(content), None)]
    finally:
        tmp_dir_ctx.cleanup()


async def test_build_bundle_directory_entries_are_not_double_prefixed(session_data, monkeypatch, tmp_path):
    # Confirmed live 2026-09-02: a restored "Downloads" directory landed
    # as "Downloads/Downloads/..." in the destination - PVE's own zip
    # for a directory selection already roots every entry under the
    # directory's own name, and this code used to prepend item.name on
    # top of that again. Locks in that info.filename is trusted as-is.
    content = b"family photo bytes"
    dir_tar = _fake_directory_tar({"photo.jpg": content}, prefix="Downloads/")
    _patch_bundle_download(monkeypatch, {"ZG93bmxvYWRz==": dir_tar})
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

    def _spying_add(writer, item, local_path, manifest, metadata, apply_ownership):
        # Snapshot how many "item-*" temp files exist in the working
        # directory at the moment each item is actually being added -
        # should never be more than the one currently being processed.
        tmp_dir_holder["dir"] = local_path.parent
        count = sum(1 for p in local_path.parent.iterdir() if p.name.startswith("item-"))
        seen_item_file_counts.append(count)
        return real_add(writer, item, local_path, manifest, metadata, apply_ownership)

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


async def test_build_bundle_manifest_omits_directory_entries_from_source_tar(session_data, monkeypatch, tmp_path):
    # A real tar from a nested directory selection often includes
    # explicit directory-marker entries - these shouldn't end up as
    # bogus manifest lines with no real file behind them.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        dir_info = tarfile.TarInfo(name="mydir/sub")
        dir_info.type = tarfile.DIRTYPE
        tf.addfile(dir_info)
        file_info = tarfile.TarInfo(name="mydir/sub/file.txt")
        file_info.size = len(b"hello")
        tf.addfile(file_info, io.BytesIO(b"hello"))
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


# --- issue #26: mtime always-on, ownership opt-in ---------------------


async def test_build_bundle_leaf_item_gets_real_mtime_unconditionally(session_data, monkeypatch, tmp_path):
    """The folded-in mtime bug fix: previously every entry landed with
    tarfile's bare default (epoch 0), regardless of the restore_
    ownership flag - mtime restoration was always meant to be
    unconditional (the modal's own copy already claimed it happened)."""
    content = b"hello"
    metadata_tar = _fake_directory_tar({"hosts": b"unused"}, mtime=1700000000)  # only the header's mtime matters
    _patch_bundle_download(monkeypatch, {"abc==": {False: content, True: metadata_tar}})
    items = [BundleItem(filepath="abc==", name="hosts", leaf=True)]

    output_path, _fmt, _manifest, tmp_dir_ctx = await build_bundle(
        session_data, "pbs:backup/vm/133/2026-09-01", items, guest_os_family="linux", zst_capable=False
    )
    try:
        with tarfile.open(output_path, mode="r:gz") as tf:
            info = tf.getmember("hosts")
            assert info.mtime == 1700000000
            assert (info.uid, info.gid, info.mode & 0o7777) == (0, 0, 0o644)  # not requested - stays default
    finally:
        tmp_dir_ctx.cleanup()


async def test_build_bundle_leaf_item_ownership_applied_only_when_requested(session_data, monkeypatch, tmp_path):
    content = b"hello"
    metadata_tar = _fake_directory_tar({"hosts": b"unused"}, uid=1000, gid=1000, mode=0o600, mtime=1700000000)
    _patch_bundle_download(monkeypatch, {"abc==": {False: content, True: metadata_tar}})
    items = [BundleItem(filepath="abc==", name="hosts", leaf=True)]

    output_path, _fmt, _manifest, tmp_dir_ctx = await build_bundle(
        session_data,
        "pbs:backup/vm/133/2026-09-01",
        items,
        guest_os_family="linux",
        zst_capable=False,
        restore_ownership=True,
    )
    try:
        with tarfile.open(output_path, mode="r:gz") as tf:
            info = tf.getmember("hosts")
            assert (info.uid, info.gid, info.mode & 0o7777) == (1000, 1000, 0o600)
            assert info.mtime == 1700000000
    finally:
        tmp_dir_ctx.cleanup()


async def test_build_bundle_directory_ownership_applied_only_when_requested(session_data, monkeypatch, tmp_path):
    dir_tar = _fake_directory_tar({"passwd": b"x"}, prefix="etc/", uid=1000, gid=1000, mode=0o600, mtime=1700000000)
    _patch_bundle_download(monkeypatch, {"etc==": dir_tar})
    items = [BundleItem(filepath="etc==", name="etc", leaf=False)]

    # Without restore_ownership: mtime is still real, but owner/mode stay tarfile defaults.
    output_path, _fmt, _manifest, tmp_dir_ctx = await build_bundle(
        session_data, "pbs:backup/vm/133/2026-09-01", items, guest_os_family="linux", zst_capable=False
    )
    try:
        with tarfile.open(output_path, mode="r:gz") as tf:
            info = tf.getmember("etc/passwd")
            assert info.mtime == 1700000000
            assert (info.uid, info.gid, info.mode & 0o7777) == (0, 0, 0o644)
    finally:
        tmp_dir_ctx.cleanup()

    # With restore_ownership: real uid/gid/mode too.
    output_path, _fmt, _manifest, tmp_dir_ctx = await build_bundle(
        session_data,
        "pbs:backup/vm/133/2026-09-01",
        items,
        guest_os_family="linux",
        zst_capable=False,
        restore_ownership=True,
    )
    try:
        with tarfile.open(output_path, mode="r:gz") as tf:
            info = tf.getmember("etc/passwd")
            assert (info.uid, info.gid, info.mode & 0o7777) == (1000, 1000, 0o600)
    finally:
        tmp_dir_ctx.cleanup()


async def test_build_bundle_restore_ownership_is_forced_off_for_windows(session_data, monkeypatch, tmp_path):
    """Defense-in-depth, same pattern as /api/restore's job-creation
    time for single-file restore (#20): NTFS has no uid/gid/mode
    concept, so this must never do anything for a Windows guest even if
    restore_ownership=True is passed in - the caller isn't trusted
    alone."""
    content = b"some content"
    _patch_bundle_download(monkeypatch, {"abc==": content})
    items = [BundleItem(filepath="abc==", name="notes.txt", leaf=True)]

    output_path, fmt, _manifest, tmp_dir_ctx = await build_bundle(
        session_data,
        "pbs:backup/vm/202/2026-09-01",
        items,
        guest_os_family="windows",
        zst_capable=False,
        restore_ownership=True,
    )
    try:
        assert fmt == BundleFormat.ZIP
        with zipfile.ZipFile(output_path) as zf:
            assert zf.read("notes.txt") == content  # just confirms it didn't blow up
    finally:
        tmp_dir_ctx.cleanup()


async def test_build_bundle_windows_directory_gets_real_mtime_from_zip(session_data, monkeypatch, tmp_path):
    """The folded-in mtime bug fix, zip/Windows side: a Windows
    directory item still downloads as PVE's default zip (tar=0), and
    its own real per-entry date_time - previously discarded - is now
    copied onto the outgoing zip entry."""
    real_date_time = (2023, 11, 14, 22, 13, 20)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        info = zipfile.ZipInfo("Downloads/photo.jpg", date_time=real_date_time)
        zf.writestr(info, b"photo bytes")
    _patch_bundle_download(monkeypatch, {"dl==": buf.getvalue()})
    items = [BundleItem(filepath="dl==", name="Downloads", leaf=False)]

    output_path, fmt, _manifest, tmp_dir_ctx = await build_bundle(
        session_data, "pbs:backup/vm/202/2026-09-01", items, guest_os_family="windows", zst_capable=False
    )
    try:
        assert fmt == BundleFormat.ZIP
        with zipfile.ZipFile(output_path) as zf:
            assert zf.getinfo("Downloads/photo.jpg").date_time == real_date_time
    finally:
        tmp_dir_ctx.cleanup()


async def test_build_bundle_directory_handles_zstd_framed_tar_too(session_data, monkeypatch, tmp_path):
    """Regression, directory side of the same live-reported bug #20's
    single-file fix addressed: PVE's tar=1 output is confirmed
    inconsistently formatted - sometimes plain tar, sometimes zstd-
    framed - for a directory item too, not just a single file."""
    dir_tar = _fake_directory_tar({"passwd": b"x"}, prefix="etc/", mtime=1700000000)
    compressed = zstandard.ZstdCompressor().compress(dir_tar)
    _patch_bundle_download(monkeypatch, {"etc==": compressed})
    items = [BundleItem(filepath="etc==", name="etc", leaf=False)]

    output_path, _fmt, _manifest, tmp_dir_ctx = await build_bundle(
        session_data, "pbs:backup/vm/133/2026-09-01", items, guest_os_family="linux", zst_capable=False
    )
    try:
        with tarfile.open(output_path, mode="r:gz") as tf:
            info = tf.getmember("etc/passwd")
            assert info.mtime == 1700000000
    finally:
        tmp_dir_ctx.cleanup()
