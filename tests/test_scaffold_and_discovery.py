"""
Unit tests for site scaffolding and dynamic adapter discovery.
"""
from pathlib import Path
from core.scaffold import scaffold_site, to_camel_case
from sites import discover_adapters
from core.adapter import BaseSiteAdapter


def test_to_camel_case():
    assert to_camel_case("twitter") == "TwitterAdapter"
    assert to_camel_case("my_super_site") == "MySuperSiteAdapter"
    assert to_camel_case("hugging-face") == "HuggingFaceAdapter"


def test_scaffold_and_discovery(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        scaffold_site("../escape", target_url="https://example.com/signup")

    test_site_name = "unit_test_demo"
    out_file = scaffold_site(test_site_name, target_url="https://example.com/signup")
    assert out_file.exists(), "Scaffold file must be created"
    assert not out_file.with_suffix(".py.tmp").exists()
    content = out_file.read_text(encoding="utf-8")
    assert "wait_for_timeout" not in content
    assert '"verify"' not in content

    try:
        adapters = discover_adapters()
        assert test_site_name in adapters or "unit_test_demo" in adapters
        adapter_cls = adapters.get(test_site_name)
        assert issubclass(adapter_cls, BaseSiteAdapter)
    finally:
        if out_file.exists():
            out_file.unlink()


def test_scaffold_auto_wires_single_imap_client():
    from core.imap_mail import SingleImapMailClient

    test_site_name = "unit_test_imap_wire"
    out_file = scaffold_site(test_site_name, target_url="https://demo.example.com/signup")
    try:
        adapters = discover_adapters()
        cls = adapters[test_site_name]
        cfg = {
            "defaults": {"use_disposable_mail": False, "country": "US", "password_length": 14},
            "mail": {
                "imap_server": "imap.example.com",
                "imap_port": 993,
                "imap_user": "alice@example.com",
                "imap_password": "app-password-demo",
            },
        }
        inst = cls(page=None, context=None, config=cfg)
        ident = inst.prepare_identity()
        assert ident["email"] == "alice@example.com"
        assert isinstance(inst.mail_client, SingleImapMailClient)
        assert inst.mail_client.server == "imap.example.com"
        assert inst.mail_client.port == 993
    finally:
        if out_file.exists():
            out_file.unlink()


def test_mail_client_list_html_and_excerpt_fallback():
    from core.mail_client import DisposableMailClient, _MailTmBackend

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"subject": "Verify", "text": "", "html": ["<p>Code: 842913</p>"]}

    import core.mail_client as mc
    orig_get = mc.requests.get
    try:
        mc.requests.get = lambda *a, **kw: _FakeResp()
        backend = _MailTmBackend(proxies=None, timeout=5)
        body = backend.body("msg-1")
        assert "842913" in body
    finally:
        mc.requests.get = orig_get

    class _ExcerptBackend:
        def messages(self):
            return [{"id": "m1", "subject": "Hello", "body": "Welcome! Click below..."}]

        def body(self, mid):
            return "Your 6-digit verification code is 559102."

    client = DisposableMailClient()
    client._backend = _ExcerptBackend()
    assert client.poll_for_verification_code(timeout_secs=5, pattern=r"\b\d{6}\b", first_interval=0.01) == "559102"
