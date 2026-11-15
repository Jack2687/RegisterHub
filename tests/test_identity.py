"""
Unit tests for synthetic identity and compliant password generation.
"""
import string
from core.identity import generate_identity, generate_compliant_password, generate_synthetic_name


def test_synthetic_name_generation():
    first, last = generate_synthetic_name("US")
    assert isinstance(first, str) and len(first) > 0
    assert isinstance(last, str) and len(last) > 0

    first_uk, last_uk = generate_synthetic_name("UK")
    assert isinstance(first_uk, str) and len(first_uk) > 0


def test_compliant_password_rules():
    pwd = generate_compliant_password(16)
    assert len(pwd) == 16
    assert any(c.isupper() for c in pwd), "Password must contain uppercase letters"
    assert any(c.islower() for c in pwd), "Password must contain lowercase letters"
    assert any(c.isdigit() for c in pwd), "Password must contain digits"
    assert any(c in "!@#$%&*_-=+" for c in pwd), "Password must contain special symbols"


def test_generate_identity_structure():
    ident = generate_identity(email_domain="test.org", locale="US")
    required_keys = ["first_name", "last_name", "username", "email", "password", "phone", "city", "state", "postal_code"]
    for k in required_keys:
        assert k in ident, f"Key '{k}' must exist in identity profile"
        assert len(str(ident[k])) > 0

    assert ident["email"].endswith("@test.org")
