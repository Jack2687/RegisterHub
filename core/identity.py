"""
Synthetic Identity Generator Pro.
Generates multi-locale realistic personas, compliant passwords, and addresses.
"""
import random
import string
from typing import Dict, Any, Tuple, Optional


LOCALES = {
    "US": {
        "first_names": [
            "James", "Alexander", "Emily", "Sophia", "Oliver", "Lucas", "Liam", "Emma", "Daniel", "Chloe",
            "Benjamin", "Nathan", "Caleb", "Ethan", "Owen", "Isaac", "Gabriel", "Julian", "Wyatt", "Carter",
            "Henry", "Samuel", "David", "Joseph", "Andrew", "Ryan", "Dylan", "Connor", "Aaron", "Evan",
            "Hannah", "Grace", "Natalie", "Claire", "Audrey", "Maya", "Zoe", "Nora", "Hazel", "Stella",
            "Rachel", "Lauren", "Megan", "Hailey", "Savannah", "Brooklyn", "Paisley", "Skylar", "Elena", "Avery",
            "Mateo", "Diego", "Marcus", "Kevin", "Trevor", "Brandon", "Justin", "Tyler", "Austin", "Jordan",
        ],
        "last_names": [
            "Walker", "Harris", "Miller", "Davis", "Wilson", "Taylor", "Anderson", "Thomas", "Jackson", "White",
            "Thompson", "Garcia", "Martinez", "Robinson", "Clark", "Rodriguez", "Lewis", "Lee", "Hall", "Allen",
            "Young", "Hernandez", "King", "Wright", "Lopez", "Hill", "Scott", "Green", "Adams", "Baker",
            "Gonzalez", "Nelson", "Carter", "Mitchell", "Perez", "Roberts", "Turner", "Phillips", "Campbell", "Parker",
            "Evans", "Edwards", "Collins", "Stewart", "Sanchez", "Morris", "Rogers", "Reed", "Cook", "Morgan",
            "Bell", "Murphy", "Bailey", "Rivera", "Cooper", "Richardson", "Cox", "Howard", "Ward", "Brooks",
        ],
        "cities": [
            ("New York", "NY", "10001"), ("San Francisco", "CA", "94105"), ("Austin", "TX", "78701"),
            ("Seattle", "WA", "98101"), ("Wilmington", "DE", "19801"), ("Portland", "OR", "97204"),
            ("Denver", "CO", "80202"), ("Boston", "MA", "02110"), ("Chicago", "IL", "60601"),
            ("Atlanta", "GA", "30303"), ("Miami", "FL", "33131"), ("Phoenix", "AZ", "85004"),
            ("Bozeman", "MT", "59715"), ("Manchester", "NH", "03101"), ("Anchorage", "AK", "99501"),
        ],
        "phone_prefix": "+1",
    },
    "UK": {
        "first_names": [
            "George", "Arthur", "Harry", "Jack", "Noah", "Leo", "Olivia", "Amelia", "Isla", "Ava",
            "Oliver", "Charlie", "Freddie", "Alfie", "Oscar", "Theo", "Archie", "Henry", "Edward", "Sebastian",
            "Freya", "Florence", "Willow", "Ivy", "Poppy", "Rosie", "Evie", "Grace", "Sophie", "Alice",
            "Callum", "Declan", "Fraser", "Gavin", "Hamish", "Kieran", "Lewis", "Nathan", "Rory", "Toby",
        ],
        "last_names": [
            "Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Johnson", "Davies", "Robinson", "Wright",
            "Thompson", "Evans", "Walker", "White", "Roberts", "Green", "Hall", "Wood", "Jackson", "Clarke",
            "Patel", "Khan", "Lewis", "James", "Phillips", "Mason", "Mitchell", "Rose", "Davis", "Cox",
            "Alexander", "Bennett", "Carter", "Edwards", "Foster", "Graham", "Hughes", "Morgan", "Parker", "Turner",
        ],
        "cities": [
            ("London", "GL", "EC1A 1BB"), ("Manchester", "GM", "M1 1AE"), ("Edinburgh", "ED", "EH1 1YZ"),
            ("Bristol", "BS", "BS1 4TR"), ("Leeds", "WY", "LS1 5AT"), ("Cambridge", "CB", "CB2 1TN"),
        ],
        "phone_prefix": "+44",
    },
    "DE": {
        "first_names": [
            "Maximilian", "Paul", "Alexander", "Elias", "Leon", "Marie", "Sophie", "Maria", "Mia", "Emma",
            "Lukas", "Jonas", "Felix", "Noah", "Ben", "Moritz", "Niklas", "Tim", "Julian", "Philipp",
            "Hannah", "Emilia", "Lina", "Clara", "Lea", "Johanna", "Laura", "Anna", "Julia", "Katharina",
            "Florian", "Tobias", "Stefan", "Matthias", "Christian", "Daniel", "Simon", "Jan", "Fabian", "Sebastian",
        ],
        "last_names": [
            "Mueller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker", "Schulz", "Hoffmann",
            "Schaefer", "Koch", "Bauer", "Richter", "Klein", "Wolf", "Schroeder", "Neumann", "Schwarz", "Zimmermann",
            "Braun", "Krueger", "Hofmann", "Hartmann", "Lange", "Schmitt", "Werner", "Schmitz", "Krause", "Meier",
            "Lehmann", "Schmid", "Schulze", "Maier", "Koehler", "Herrmann", "Koenig", "Walter", "Mayer", "Huber",
        ],
        "cities": [
            ("Berlin", "BE", "10115"), ("Munich", "BY", "80331"), ("Hamburg", "HH", "20095"),
            ("Frankfurt", "HE", "60311"), ("Cologne", "NW", "50667"), ("Stuttgart", "BW", "70173"),
        ],
        "phone_prefix": "+49",
    },
}

_ADJECTIVES = [
    "north", "amber", "cedar", "civic", "delta", "fable", "harbor", "ivory", "linen", "maple",
    "orbit", "pine", "quartz", "ridge", "slate", "timber", "urban", "velvet", "willow", "zenith",
]
_NOUNS = [
    "ops", "studio", "craft", "works", "notes", "bench", "stack", "field", "ml", "cloud",
    "build", "grid", "track", "desk", "forge", "scale", "core", "kit", "mesh", "node",
]


def generate_synthetic_name(locale: str = "US") -> Tuple[str, str]:
    """Return realistic first and last names matching target locale."""
    data = LOCALES.get(locale.upper(), LOCALES["US"])
    first = random.choice(data["first_names"])
    last = random.choice(data["last_names"])
    return first, last


def generate_compliant_password(length: int = 14) -> str:
    """
    Generate an enterprise-compliant password satisfying all modern password complexity rules:
    - At least 1 uppercase letter
    - At least 1 lowercase letter
    - At least 1 number
    - At least 2 special symbols
    - High entropy distribution
    """
    if length < 10:
        length = 14
    lower = random.choice(string.ascii_lowercase)
    upper = random.choice(string.ascii_uppercase)
    digit1 = random.choice(string.digits)
    digit2 = random.choice(string.digits)
    sym1 = random.choice("!@#$%&*")
    sym2 = random.choice("_-=+")
    
    pool = string.ascii_letters + string.digits + "!@#$%&*_-=+"
    remaining = [random.choice(pool) for _ in range(length - 6)]
    
    chars = [lower, upper, digit1, digit2, sym1, sym2] + remaining
    random.shuffle(chars)
    return "".join(chars)


def generate_username(first: str, last: str) -> str:
    """Generate a natural, non-clustered human handle across diverse real-world styles."""
    f_low = first.lower()
    l_low = last.lower()
    yr = random.randint(84, 99)
    num = random.randint(10, 999)
    patterns = [
        f"{f_low}-{l_low}",
        f"{f_low[0]}{l_low}{yr}",
        f"{f_low}.{l_low[0]}{num}",
        f"{f_low}_{l_low[:4]}{yr}",
        f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}{random.randint(1, 99)}",
        f"{f_low}{l_low[:3]}{num}",
        f"{l_low}.{f_low[:3]}{yr}",
    ]
    return random.choice(patterns)


def generate_identity(
    email_domain: Optional[str] = None,
    locale: str = "US",
    use_disposable: bool = False,
    mail_client: Optional[Any] = None,
    password_length: int = 14,
) -> Dict[str, Any]:
    """Generate a cohesive, multi-field identity profile.

    If mail_client is provided, delegates mailbox creation to it.
    Else if use_disposable is True, dynamically allocates a real mailbox via DisposableMailClient.
    If email_domain is given, constructs an address on that domain.
    Otherwise uses an explicit mock domain (@local.dev) to avoid fabricating unowned mail domains.
    """
    loc_data = LOCALES.get(locale.upper(), LOCALES["US"])
    first, last = generate_synthetic_name(locale)
    username = generate_username(first, last)
    password = generate_compliant_password(password_length)

    if mail_client is not None:
        email = mail_client.create_mailbox()
    elif use_disposable:
        from core.mail_client import DisposableMailClient
        client = DisposableMailClient()
        email = client.create_mailbox()
    elif email_domain:
        email = f"{username}@{email_domain}"
    else:
        email = f"{username}@local.dev"

    city, state, postal = random.choice(loc_data["cities"])
    phone_suffix = "".join(random.choices(string.digits, k=10))
    phone = f"{loc_data['phone_prefix']}{phone_suffix}"

    birth_year = random.randint(1988, 2004)
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)

    return {
        "locale": locale.upper(),
        "first_name": first,
        "last_name": last,
        "full_name": f"{first} {last}",
        "username": username,
        "email": email,
        "password": password,
        "phone": phone,
        "city": city,
        "state": state,
        "postal_code": postal,
        "birth_date": f"{birth_year}-{birth_month:02d}-{birth_day:02d}"
    }
