"""
Synthetic Identity Generator.
Generates realistic credentials, emails, and compliant passwords.
"""
import random
import string
from typing import Dict, Any


_FIRST_NAMES = [
    "James", "Alexander", "Emily", "Sophia", "Oliver", "Lucas", "Liam",
    "Emma", "Charlotte", "Daniel", "Matthew", "Benjamin", "Chloe", "Grace"
]

_LAST_NAMES = [
    "Walker", "Harris", "Miller", "Davis", "Wilson", "Taylor", "Anderson",
    "Thomas", "Jackson", "White", "Harris", "Martin", "Thompson", "Garcia"
]

_ADJECTIVES = ["cool", "swift", "hyper", "cyber", "nova", "prime", "alpha", "zen", "cloud"]
_NOUNS = ["dev", "coder", "lab", "hub", "tech", "wave", "pilot", "spark", "flow"]


def generate_synthetic_name() -> tuple[str, str]:
    """Return a realistic first name and last name."""
    first = random.choice(_FIRST_NAMES)
    last = random.choice(_LAST_NAMES)
    return first, last


def generate_compliant_password(length: int = 14) -> str:
    """Generate a password with uppercase, lowercase, digits, and special characters."""
    if length < 8:
        length = 12
    lower = random.choice(string.ascii_lowercase)
    upper = random.choice(string.ascii_uppercase)
    digit = random.choice(string.digits)
    symbol = random.choice("!@#$%&*_-")
    
    pool = string.ascii_letters + string.digits + "!@#$%&*_-="
    remaining = [random.choice(pool) for _ in range(length - 4)]
    
    chars = [lower, upper, digit, symbol] + remaining
    random.shuffle(chars)
    return "".join(chars)


def generate_username(first: str, last: str) -> str:
    """Generate a coherent, believable username derived from name and tech terms."""
    adj = random.choice(_ADJECTIVES)
    num = random.randint(10, 999)
    patterns = [
        f"{first.lower()}_{last.lower()}{num}",
        f"{first.lower()}.{adj}{num}",
        f"{last.lower()}_{random.choice(_NOUNS)}{num}",
        f"{adj}_{first.lower()}{num}"
    ]
    return random.choice(patterns)


def generate_identity(email_domain: str = "example.com") -> Dict[str, Any]:
    """Generate a cohesive identity profile."""
    first, last = generate_synthetic_name()
    username = generate_username(first, last)
    password = generate_compliant_password()
    email = f"{username}@{email_domain}"
    birth_year = random.randint(1988, 2004)
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    
    return {
        "first_name": first,
        "last_name": last,
        "full_name": f"{first} {last}",
        "username": username,
        "email": email,
        "password": password,
        "birth_date": f"{birth_year}-{birth_month:02d}-{birth_day:02d}"
    }
