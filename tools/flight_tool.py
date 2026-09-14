"""
flight_search.py
----------------
Natural-language live flight search powered by the AviationStack API.

Example queries:
    - "Plan a 7 days india to russia trip "
    - "flights from indore to paris"
    - "to Thailand"
    - "all country flight info"
"""

# ---------- Imports ----------
import os
import re

import certifi
import requests
import pycountry
import airportsdata
from dotenv import load_dotenv

# ---------- Environment / config ----------
load_dotenv()  # Load variables from .env into os.environ

# Ensure requests uses certifi's CA bundle (fixes SSL errors on some systems)
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

API_KEY = os.getenv("AVIATIONSTACK_API_KEY")               # Required API key
DEFAULT_ORIGIN_IATA = os.getenv("DEFAULT_ORIGIN_IATA", "JFK")  # Fallback origin

BASE_URL = "http://api.aviationstack.com/v1/flights"       # Free tier = HTTP only

# IATA-keyed airport dictionary (e.g. {"DAC": {...}, "NRT": {...}})
AIRPORTS = airportsdata.load()


# ---------- Lookup tables ----------

# Country name / nickname -> ISO alpha-2 code
COUNTRY_ALIASES = {
    "usa": "US",
    "u.s.a": "US",
    "u.s.": "US",
    "america": "US",
    "united states": "US",
    "uk": "GB",
    "u.k.": "GB",
    "britain": "GB",
    "england": "GB",
    "uae": "AE",
    "dubai": "AE",
    "south korea": "KR",
    "korea": "KR",
    "russia": "RU",
    "vietnam": "VN",
    "bangladesh": "BD",
    "india": "IN",
    "japan": "JP",
    "china": "CN",
    "singapore": "SG",
    "malaysia": "MY",
    "thailand": "TH",
    "indonesia": "ID",
    "nepal": "NP",
    "qatar": "QA",
    "saudi arabia": "SA",
    "turkey": "TR",
    "canada": "CA",
    "australia": "AU",
    "germany": "DE",
    "france": "FR",
    "italy": "IT",
    "spain": "ES",
}

# Preferred main airport for each country (used for country-level queries)
COUNTRY_MAIN_AIRPORT = {
    "BD": "DAC",
    "IN": "DEL",
    "JP": "NRT",
    "US": "JFK",
    "GB": "LHR",
    "AE": "DXB",
    "SG": "SIN",
    "MY": "KUL",
    "TH": "BKK",
    "ID": "CGK",
    "CN": "PEK",
    "KR": "ICN",
    "NP": "KTM",
    "QA": "DOH",
    "SA": "JED",
    "TR": "IST",
    "CA": "YYZ",
    "AU": "SYD",
    "DE": "FRA",
    "FR": "CDG",
    "IT": "FCO",
    "ES": "MAD",
}

# Preferred main airport for each well-known city
CITY_MAIN_AIRPORT = {
    "dhaka": "DAC",
    "delhi": "DEL",
    "new delhi": "DEL",
    "mumbai": "BOM",
    "kolkata": "CCU",
    "chennai": "MAA",
    "bangalore": "BLR",
    "bengaluru": "BLR",
    "tokyo": "NRT",
    "osaka": "KIX",
    "kyoto": "KIX",
    "new york": "JFK",
    "london": "LHR",
    "dubai": "DXB",
    "singapore": "SIN",
    "kuala lumpur": "KUL",
    "bangkok": "BKK",
    "doha": "DOH",
    "istanbul": "IST",
    "toronto": "YYZ",
    "sydney": "SYD",
    "paris": "CDG",
    "rome": "FCO",
    "madrid": "MAD",
    "frankfurt": "FRA",
}

# Words that carry no location information and can be dropped
STOP_WORDS = [
    "flight", "flights", "ticket", "tickets", "trip", "travel",
    "plan", "complete", "days", "day", "including", "hotel",
    "hotels", "sightseeing", "under", "budget", "info", "information",
]


# ---------- Text utilities ----------

def clean_text(text: str) -> str:
    """Lowercase, strip punctuation, and remove common travel stopwords."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s]", " ", text)   # Drop punctuation / symbols
    text = re.sub(r"\s+", " ", text)           # Collapse multiple spaces
    words = [w for w in text.split() if w not in STOP_WORDS]
    return " ".join(words).strip()


# ---------- Country resolution ----------

def country_name_to_code(text: str):
    """Resolve a country name or alias to its ISO alpha-2 code."""
    text = clean_text(text)
    if not text:
        return None

    # Direct alias hit (fast path)
    if text in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[text]

    # Direct pycountry lookup (e.g. "Portugal")
    try:
        country = pycountry.countries.lookup(text)
        return country.alpha_2
    except LookupError:
        pass

    # Full country name inside a longer phrase (word-boundary safe)
    for country in pycountry.countries:
        country_name = country.name.lower()
        if re.search(rf"\b{re.escape(country_name)}\b", text):
            return country.alpha_2

    # Alias inside a longer phrase (word-boundary safe)
    for alias, code in COUNTRY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", text):
            return code

    return None


def airport_country_matches(airport: dict, country_code: str) -> bool:
    """True if the airport's country equals the given ISO alpha-2 code."""
    # airportsdata stores country as ISO alpha-2, so just compare
    return str(airport.get("country", "")).upper().strip() == country_code.upper()


def get_best_airport_for_country(country_code: str):
    """Return a preferred IATA code for a country, or pick the best-scored one."""
    # Prefer our hand-curated table
    preferred = COUNTRY_MAIN_AIRPORT.get(country_code)
    if preferred and preferred in AIRPORTS:
        return preferred

    # Fallback: score every airport in the country and pick the highest
    candidates = []
    for iata, airport in AIRPORTS.items():
        if not iata:
            continue
        if airport_country_matches(airport, country_code):
            name = str(airport.get("name", "")).lower()
            city = str(airport.get("city", "")).lower()

            score = 0
            if "international" in name:
                score += 50
            if "intl" in name:
                score += 40
            if "capital" in name:
                score += 20
            if city:
                score += 5

            candidates.append((score, iata))

    if not candidates:
        return None

    candidates.sort(reverse=True)
    return candidates[0][1]


# ---------- Location -> IATA ----------

def resolve_location_to_iata(location: str):
    """Convert country / city / airport / IATA into an IATA code."""
    if not location:
        return None

    raw_location = location.strip()

    # Case 1: 3-letter code already (e.g. "DAC")
    if re.fullmatch(r"[A-Za-z]{3}", raw_location):
        code = raw_location.upper()
        if code in AIRPORTS:
            return code

    location_clean = clean_text(raw_location)
    if not location_clean:
        return None

    # Case 2: known city -> preferred airport
    if location_clean in CITY_MAIN_AIRPORT:
        return CITY_MAIN_AIRPORT[location_clean]

    # Case 3: country name -> preferred / best airport
    country_code = country_name_to_code(location_clean)
    if country_code:
        airport = get_best_airport_for_country(country_code)
        if airport:
            return airport

    # Case 4: fuzzy match against airport city / name fields
    city_matches = []
    for iata, airport in AIRPORTS.items():
        city = str(airport.get("city", "")).lower().strip()
        name = str(airport.get("name", "")).lower().strip()

        score = 0
        if city == location_clean:
            score += 100
        elif location_clean in city:
            score += 70
        if location_clean in name:
            score += 50
        if "international" in name:
            score += 10

        if score > 0:
            city_matches.append((score, iata))

    if city_matches:
        city_matches.sort(reverse=True)
        return city_matches[0][1]

    return None


# ---------- Query -> route ----------

def find_location_mentions(query: str):
    """Find country / city mentions, ordered by position in the query."""
    q = query.lower()
    found = []  # list of (position, name)

    # Match each alias with word boundaries
    for alias in COUNTRY_ALIASES:
        m = re.search(rf"\b{re.escape(alias)}\b", q)
        if m:
            found.append((m.start(), alias))

    # Match each full country name (skip very short names to avoid noise)
    for country in pycountry.countries:
        name = country.name.lower()
        if len(name) >= 4:
            m = re.search(rf"\b{re.escape(name)}\b", q)
            if m:
                found.append((m.start(), name))

    # Match each known city
    for city in CITY_MAIN_AIRPORT:
        m = re.search(rf"\b{re.escape(city)}\b", q)
        if m:
            found.append((m.start(), city))

    # Sort by position so origin/destination order is preserved
    found.sort(key=lambda x: x[0])

    # Dedupe keeping first occurrence
    unique_mentions = []
    for _, name in found:
        if name not in unique_mentions:
            unique_mentions.append(name)

    return unique_mentions


def parse_route(query: str):
    """
    Parse a natural-language query into (dep_iata, arr_iata).

    Returns:
        None, None  -> global flights
        DAC, NRT    -> specific route
        DAC, None   -> all flights from DAC
        None, NRT   -> all flights to NRT
    """
    q = query.strip()
    q_lower = q.lower()

    # Short-circuit for global queries
    global_keywords = [
        "all country", "all countries",
        "global flight", "global flights",
        "all flight", "all flights",
        "worldwide flight", "worldwide flights",
    ]
    if any(keyword in q_lower for keyword in global_keywords):
        return None, None

    # Fast path: two valid IATA codes in the query
    codes = re.findall(r"\b[A-Za-z]{3}\b", q)
    valid_codes = [c.upper() for c in codes if c.upper() in AIRPORTS]
    if len(valid_codes) >= 2:
        return valid_codes[0], valid_codes[1]

    # Pattern: "from X to Y"
    match = re.search(
        r"\bfrom\s+(.+?)\s+\bto\s+(.+?)(?:\s+(?:on|for|under|including|with|in|at|next|this)\b|[.!?]|$)",
        q_lower,
    )
    if match:
        return resolve_location_to_iata(match.group(1)), resolve_location_to_iata(match.group(2))

    # Pattern: "to Y from X"
    match = re.search(
        r"\bto\s+(.+?)\s+\bfrom\s+(.+?)(?:\s+(?:on|for|under|including|with|in|at|next|this)\b|[.!?]|$)",
        q_lower,
    )
    if match:
        return resolve_location_to_iata(match.group(2)), resolve_location_to_iata(match.group(1))

    # Pattern: "from X" (origin only)
    match = re.search(r"\bfrom\s+(.+?)(?:[.!?]|$)", q_lower)
    if match:
        dep_iata = resolve_location_to_iata(match.group(1))
        if dep_iata:
            return dep_iata, None

    # Pattern: "to X" (destination only)
    match = re.search(r"\bto\s+(.+?)(?:[.!?]|$)", q_lower)
    if match:
        arr_iata = resolve_location_to_iata(match.group(1))
        if arr_iata:
            return None, arr_iata

    # Fallback: scan for country / city mentions
    mentions = find_location_mentions(q)

    if len(mentions) >= 2:
        dep_iata = resolve_location_to_iata(mentions[0])
        arr_iata = resolve_location_to_iata(mentions[1])
        if dep_iata and arr_iata:
            return dep_iata, arr_iata

    if len(mentions) == 1:
        arr_iata = resolve_location_to_iata(mentions[0])
        if arr_iata:
            # Only destination given -> use default origin
            return DEFAULT_ORIGIN_IATA, arr_iata

    return None, None


# ---------- Output formatting ----------

def format_flight(flight: dict) -> str:
    """Pretty-print a single flight record."""
    airline = flight.get("airline", {}).get("name") or "Unknown airline"
    flight_number = flight.get("flight", {}).get("iata") or "Unknown flight number"
    status = flight.get("flight_status") or "Unknown"

    dep = flight.get("departure", {}) or {}
    arr = flight.get("arrival", {}) or {}

    # Human-readable delay strings
    dep_delay = dep.get("delay")
    dep_delay_text = f"{dep_delay} minutes" if dep_delay is not None else "N/A"

    arr_delay = arr.get("delay")
    arr_delay_text = f"{arr_delay} minutes" if arr_delay is not None else "N/A"

    return f"""
Airline: {airline}
Flight: {flight_number}
Status: {status}

Departure:
- Airport: {dep.get("airport") or "Unknown departure airport"}
- IATA: {dep.get("iata") or "Unknown"}
- Terminal: {dep.get("terminal") or "N/A"}
- Gate: {dep.get("gate") or "N/A"}
- Scheduled: {dep.get("scheduled") or "Unknown"}
- Delay: {dep_delay_text}

Arrival:
- Airport: {arr.get("airport") or "Unknown arrival airport"}
- IATA: {arr.get("iata") or "Unknown"}
- Terminal: {arr.get("terminal") or "N/A"}
- Gate: {arr.get("gate") or "N/A"}
- Scheduled: {arr.get("scheduled") or "Unknown"}
- Delay: {arr_delay_text}
""".strip()


# ---------- Public API ----------

def search_flights(query: str, limit: int = 10) -> str:
    """Search live flights using a natural-language query."""
    # Guard against missing API key
    if not API_KEY:
        return (
            "Flight API error: AVIATIONSTACK_API_KEY is missing.\n"
            "Please add this in your .env file:\n"
            "AVIATIONSTACK_API_KEY=your_api_key_here"
        )

    dep_iata, arr_iata = parse_route(query)

    # Build request parameters (only include filters we actually have)
    params = {
        "access_key": API_KEY,
        "limit": min(limit, 100),  # API caps limit at 100
    }
    if dep_iata:
        params["dep_iata"] = dep_iata
    if arr_iata:
        params["arr_iata"] = arr_iata

    # Call the API
    try:
        response = requests.get(BASE_URL, params=params, timeout=30)
        data = response.json()
    except requests.exceptions.RequestException as e:
        return f"Flight API request failed: {e}"
    except ValueError:
        return "Flight API returned invalid JSON."

    # API-level error
    if "error" in data:
        error = data["error"]
        return (
            "Flight API error:\n"
            f"Code: {error.get('code', 'Unknown')}\n"
            f"Message: {error.get('message', 'Unknown error')}"
        )

    flight_data = data.get("data", [])

    # No results -> descriptive message
    if not flight_data:
        route_text = ""
        if dep_iata and arr_iata:
            route_text = f" for route {dep_iata} to {arr_iata}"
        elif dep_iata:
            route_text = f" from {dep_iata}"
        elif arr_iata:
            route_text = f" to {arr_iata}"

        return (
            f"No live flight data found{route_text}.\n\n"
            "Note: AviationStack provides live/status flight data, not ticket prices. "
            "For actual fare prices, use a flight-pricing API such as Amadeus."
        )

    # Build a header describing what was searched
    if dep_iata and arr_iata:
        route_info = f"Live flights from {dep_iata} to {arr_iata}"
    elif dep_iata:
        route_info = f"Live flights from {dep_iata}"
    elif arr_iata:
        route_info = f"Live flights to {arr_iata}"
    else:
        route_info = "Global live flights"

    formatted_flights = [format_flight(flight) for flight in flight_data[:limit]]
    return f"{route_info}\n\n" + "\n\n---\n\n".join(formatted_flights)


# ---------- CLI entry point ----------

if __name__ == "__main__":
    print(search_flights("Plan a 7 days Japan trip from Bangladesh"))
    print("\n" + "=" * 80 + "\n")
    print(search_flights("all country flight info"))