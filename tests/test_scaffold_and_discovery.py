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
    test_site_name = "unit_test_demo"
    out_file = scaffold_site(test_site_name, target_url="https://example.com/signup")
    assert out_file.exists(), "Scaffold file must be created"
    
    try:
        adapters = discover_adapters()
        assert test_site_name in adapters or "unit_test_demo" in adapters
        adapter_cls = adapters.get(test_site_name)
        assert issubclass(adapter_cls, BaseSiteAdapter)
    finally:
        if out_file.exists():
            out_file.unlink()
