import pytest

main = pytest.importorskip("backend.main", reason="backend.main needs FastAPI")


@pytest.mark.parametrize(
    "volid, expected",
    [
        ("pbs:backup/vm/133/2026-08-30T02:03:57Z", ("vm", "133", "2026-08-30T02:03:57Z")),
        ("pbs:backup/ct/104/2026-01-02T00:00:00Z", ("ct", "104", "2026-01-02T00:00:00Z")),
    ],
)
def test_parse_volid(volid, expected):
    assert main._parse_volid(volid) == expected


def test_type_label_root_vs_nested():
    folder = {"leaf": False, "text": "etc"}
    assert main._type_label(folder, at_root=True) == "Drive"
    assert main._type_label(folder, at_root=False) == "Folder"


def test_type_label_files():
    assert main._type_label({"leaf": True, "text": "notes.txt"}, at_root=False) == "TXT File"
    assert main._type_label({"leaf": True, "text": "README"}, at_root=False) == "File"


def test_content_disposition_escapes_and_encodes():
    header = main._content_disposition('a"b\r\n.txt')
    assert header.startswith('attachment; filename="')
    assert "\r" not in header and "\n" not in header
    assert "filename*=UTF-8''" in header


def test_content_disposition_unicode():
    header = main._content_disposition("résúmé.pdf")
    assert "filename*=UTF-8''r%C3%A9s%C3%BAm%C3%A9.pdf" in header


def test_pve_error_message_prefers_json_message():
    class FakeResp:
        reason_phrase = "Bad Request"

        def json(self):
            return {"message": "no such volume"}

    exc = _fake_status_error(FakeResp())
    assert main._pve_error_message(exc) == "no such volume"


def test_pve_error_message_uses_reason_phrase():
    class FakeResp:
        reason_phrase = "Forbidden"

        def json(self):
            raise ValueError

    assert main._pve_error_message(_fake_status_error(FakeResp())) == "Forbidden"


def test_static_version_missing_file_is_zero():
    assert main._static_version("does-not-exist.js") == 0


def test_static_version_existing_file_is_mtime():
    assert main._static_version("app.js") > 0


def test_fromtimestamp_filter():
    f = main.templates.env.filters["fromtimestamp"]
    assert f(0) == "1970-01-01 00:00:00 UTC"
    assert f(None) == ""


def _fake_status_error(response):
    import httpx

    err = httpx.HTTPStatusError("x", request=None, response=None)
    err.response = response
    return err
