"""Eigenaar en openbaar organisatiecontact bij een gescrapet adres.

Het open kadastrale kaartbestand van PDOK levert het perceel, niet de
tenaamstelling. Eigenaar komt alleen uit een Kadaster-product waarvoor
KADASTER_API_KEY is gezet:

- objectinformatie (standaard). Kadata Objectinformatie, productcode
  "rechten", € 2,96 per adres, vrij van btw. Abonnement: Mijn Kadaster met
  Kadata Internet. Sleutel aanvragen in Kadata Internet.
  POST https://kadatawebservice.kadaster.nl/objectinformatieapi/api/v1/report
  Header: X-API-KEY
- contractloos. € 0,82 per adres. Zelfde abonnement. Bedoeld voor
  nutsbedrijven die een eigenaar moeten aanschrijven zonder leveringscontract.
  GET https://kadatawebservice.kadaster.nl/contractloos/api/v1/
- brk. Alleen voor overheidsorganisaties die BSN-gerechtigd zijn, met
  API-sleutel en doorgaans een PKIoverheid-certificaat.
  https://api.brk.kadaster.nl/esd/bevragen/v2/

Geen van die producten levert e-mail of telefoon.

Voor een bedrijf, stichting, gemeente of andere rechtspersoon vullen de
KVK-API's (developers.kvk.nl, header apikey) het KVK-nummer, de statutaire
naam, het bezoekadres en geregistreerde websites aan. Het handelsregister
bevat geen e-mail en geen telefoon. Die worden gelezen van de eigen website
van de organisatie. Wikidata is de openbare terugval voor de officiële
website en voor contactgegevens die niet op de site staan.

Van een particulier worden geen e-mail, telefoon of huisadres opgezocht.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

USER_AGENT = "Scraper/0.2 (lokaal; BAG, Kadaster en openbare organisatiecontacten)"
PDOK_BRK = "https://api.pdok.nl/kadaster/brk-kadastrale-kaart/ogc/v1"
KVK_BASE = "https://api.kvk.nl"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
OBJECTINFORMATIE_URL = (
    "https://kadatawebservice.kadaster.nl/objectinformatieapi/api/v1/report"
)
CONTRACTLOOS_BASE = "https://kadatawebservice.kadaster.nl/contractloos"
BRK_BASE = "https://api.brk.kadaster.nl/esd/bevragen/v2"

REASON_API = "API does not provide this information."
REASON_AUTH = "Authentication required."
REASON_PAID = "Paid subscription required."
REASON_PRIVATE = "Information not publicly available."
REASON_NO_MATCH = "No matching organization found."

_ENV_LOADED = False
_PAGE_CACHE: dict[str, str] = {}


def load_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    path = Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env(name: str) -> str:
    load_env()
    return (os.environ.get(name) or "").strip()


def blank() -> dict:
    return {
        "ownership": {
            "owner_name": "",
            "owner_type": "",
            "kvk_number": "",
            "parcel": "",
            "parcel_id": "",
            "right": "",
            "source": "",
        },
        "contact_information": {
            "company_name": "",
            "website": "",
            "email": "",
            "phone": "",
            "business_address": "",
            "contact_person": "",
        },
        "contact_source": "",
        "contact_verified": False,
        "unavailable": [],
    }


def note(field: str, reason: str, detail: str) -> dict:
    return {"field": field, "reason": reason, "detail": detail}


def add_note(report: dict, field: str, reason: str, detail: str) -> None:
    entry = note(field, reason, detail)
    if entry not in report["unavailable"]:
        report["unavailable"].append(entry)


def failed(detail: str) -> dict:
    report = blank()
    add_note(report, "contact_information", REASON_API, detail)
    return report


def request_bytes(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    body: dict | None = None,
    timeout: int = 20,
    context: ssl.SSLContext | None = None,
) -> tuple[int, bytes, str]:
    data = None
    outgoing = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        outgoing.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        outgoing.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=outgoing, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return response.status, response.read(), response.geturl()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), url


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    body: dict | None = None,
    timeout: int = 20,
    context: ssl.SSLContext | None = None,
) -> tuple[int, dict | list | None, str]:
    status, raw, final = request_bytes(
        url, method=method, headers=headers, body=body, timeout=timeout, context=context
    )
    if not raw:
        return status, None, ""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = raw.decode("utf-8", "replace")[:300]
        return status, None, text
    message = ""
    if isinstance(parsed, dict):
        message = str(parsed.get("message") or parsed.get("title") or "")[:300]
    return status, parsed, message


def redact(text: str, secret: str) -> str:
    if secret and text:
        return text.replace(secret, "[afgeschermd]")
    return text


def norm_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def fold(value: str) -> str:
    text = (value or "").lower().replace("&", " en ")
    text = text.replace("ö", "o").replace("é", "e").replace("ë", "e").replace("ï", "i")
    text = re.sub(
        r"\b(b\.?\s*v\.?|n\.?\s*v\.?|v\.?\s*o\.?\s*f\.?|c\.?\s*v\.?|u\.?\s*a\.?)\b",
        " ",
        text,
    )
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return norm_space(text)


def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def waarde(raw) -> str:
    if isinstance(raw, dict):
        return str(raw.get("waarde") or raw.get("value") or raw.get("code") or "").strip()
    if raw is None:
        return ""
    return str(raw).strip()


def starts_with_word(text: str, word: str) -> bool:
    return text == word or text.startswith(word + " ")


def classify(name: str, rechtsvorm: str, *, natural_person: bool, from_entity: bool) -> str:
    if natural_person:
        return "private_individual"
    raw = f"{rechtsvorm or ''} {name or ''}".lower()
    form = fold(rechtsvorm)
    label = fold(name)
    blob = f"{form} {label}".strip()
    if "gemeente" in form or starts_with_word(label, "gemeente"):
        return "municipality"
    if any(
        token in blob
        for token in (
            "provincie",
            "waterschap",
            "ministerie",
            "staat der nederlanden",
            "rijkswaterstaat",
            "rijksoverheid",
        )
    ):
        return "government"
    if starts_with_word(label, "stichting") or "stichting" in form:
        return "foundation"
    if starts_with_word(label, "vereniging") or "vereniging" in form:
        return "association"
    company_tokens = (
        "besloten vennootschap",
        "naamloze vennootschap",
        "cooperatie",
        "vennootschap onder firma",
        "commanditaire",
        "maatschap",
        "eenmanszaak",
    )
    if any(token in form or f" {token} " in f" {blob} " for token in company_tokens) or re.search(
        r"\b(b\.?\s*v\.?|n\.?\s*v\.?|v\.?\s*o\.?\s*f\.?|c\.?\s*v\.?)\b", raw
    ):
        return "company"
    if from_entity or form:
        return "other_legal_entity"
    return "unknown"


def format_address(parts: dict) -> str:
    street = norm_space(str(parts.get("street") or parts.get("straatnaam") or parts.get("openbareRuimteNaam") or ""))
    number = str(parts.get("housenumber") or parts.get("huisnummer") or "").strip()
    letter = str(parts.get("housenumberLetter") or parts.get("huisletter") or parts.get("huisletter") or "").strip()
    suffix = str(parts.get("suffix") or parts.get("huisnummerToevoeging") or parts.get("huisnummertoevoeging") or "").strip()
    postcode = str(parts.get("postcode") or "").replace(" ", "").upper()
    if len(postcode) == 6:
        postcode = postcode[:4] + " " + postcode[4:]
    city = norm_space(str(parts.get("city") or parts.get("plaats") or ""))
    number_bit = f"{number}{letter}"
    if suffix:
        number_bit = f"{number_bit}-{suffix}" if number_bit else suffix
    line = norm_space(f"{street} {number_bit}")
    tail = norm_space(f"{postcode} {city}")
    foreign = norm_space(str(parts.get("adres") or ""))
    if foreign and not line:
        land = norm_space(str(parts.get("land") or parts.get("country") or ""))
        return ", ".join(part for part in (foreign, city, land) if part)
    return ", ".join(part for part in (line, tail) if part)


def postcode_huis(doc: dict) -> dict:
    postcode = str(doc.get("postcode") or "").replace(" ", "").upper()
    number = str(doc.get("huisnummer") or "").strip()
    letter = str(doc.get("huisletter") or "").strip().upper()
    suffix = str(doc.get("huisnummertoevoeging") or "").strip()
    return {
        "postcode": postcode if re.fullmatch(r"[1-9][0-9]{3}[A-Z]{2}", postcode) else "",
        "number": number if re.fullmatch(r"\d{1,5}", number) else "",
        "letter": letter if re.fullmatch(r"[A-Z]", letter) else "",
        "suffix": suffix if re.fullmatch(r"[A-Za-z0-9]{1,4}", suffix) else "",
        "nummeraanduiding": str(doc.get("nummeraanduiding_id") or ""),
        "verblijfsobject": str(doc.get("adresseerbaarobject_id") or ""),
        "city": str(doc.get("woonplaatsnaam") or doc.get("gemeentenaam") or ""),
    }


def parse_perceelcode(value: str) -> tuple[str, str, str] | None:
    match = re.fullmatch(r"([A-Z0-9]{3,6})[-\s]+([A-Z]{1,2})[-\s]+(\d{1,5})", value.strip().upper())
    if not match:
        return None
    return match.group(1), match.group(2), str(int(match.group(3)))


def point_in_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    if len(ring) < 3:
        return False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous[0], previous[1]
        x2, y2 = current[0], current[1]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
        previous = current
    return inside


def geometry_contains(geometry: dict, lon: float, lat: float) -> bool:
    if not geometry:
        return False
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if kind == "Polygon":
        return bool(coordinates) and point_in_ring(lon, lat, coordinates[0])
    if kind == "MultiPolygon":
        return any(polygon and point_in_ring(lon, lat, polygon[0]) for polygon in coordinates)
    return False


def pdok_items(params: dict) -> list[dict]:
    query = urllib.parse.urlencode({"f": "json", "limit": "10", **params})
    status, payload, _message = request_json(f"{PDOK_BRK}/collections/perceel/items?{query}", timeout=20)
    if status != 200 or not isinstance(payload, dict):
        return []
    return payload.get("features") or []


def feature_parcel(feature: dict) -> dict:
    props = feature.get("properties") or {}
    gemeente = str(props.get("kadastrale_gemeente_waarde") or "").strip()
    sectie = str(props.get("sectie") or "").strip()
    nummer = str(props.get("perceelnummer") or "").strip()
    namespace = str(props.get("identificatie_namespace") or "").strip()
    local_id = str(props.get("identificatie_lokaal_id") or "").strip()
    designation = " ".join(part for part in (gemeente, sectie, nummer) if part)
    full_id = ".".join(part for part in (namespace, local_id) if part)
    return {
        "designation": designation,
        "id": full_id,
        "gemeente": gemeente,
        "sectie": sectie,
        "nummer": nummer,
        "code": str(props.get("akr_kadastrale_gemeente_code_waarde") or ""),
    }


def resolve_parcel(doc: dict) -> dict | None:
    raw = doc.get("gekoppeld_perceel") or []
    if isinstance(raw, str):
        raw = [raw]
    for item in raw[:3]:
        parsed = parse_perceelcode(str(item))
        if not parsed:
            continue
        code, sectie, nummer = parsed
        features = pdok_items(
            {
                "akr_kadastrale_gemeente_code_waarde": code,
                "sectie": sectie,
                "perceelnummer": nummer,
                "limit": "1",
            }
        )
        if features:
            return feature_parcel(features[0])
        return {"designation": f"{code} {sectie} {nummer}", "id": "", "gemeente": "", "sectie": sectie, "nummer": nummer, "code": code}

    point = None
    match = re.search(r"POINT\(([0-9.]+)\s+([0-9.]+)\)", str(doc.get("centroide_ll") or ""))
    if match:
        point = float(match.group(1)), float(match.group(2))
    if not point:
        return None
    lon, lat = point
    delta = 0.0008
    features = pdok_items(
        {
            "bbox": f"{lon - delta},{lat - delta},{lon + delta},{lat + delta}",
            "limit": "15",
        }
    )
    for feature in features:
        if geometry_contains(feature.get("geometry") or {}, lon, lat):
            return feature_parcel(feature)
    if len(features) == 1:
        return feature_parcel(features[0])
    return None


def kadaster_failure(report: dict, status: int, message: str, secret: str) -> None:
    detail = redact(message, secret)
    if status in (401, 403):
        reason = REASON_PAID if "10.000" in detail or "10000" in detail else REASON_AUTH
        add_note(
            report,
            "ownership.owner_name",
            reason,
            f"Kadaster antwoordde met HTTP {status}. {detail or 'De sleutel is ongeldig of hoort niet bij dit product.'}",
        )
        return
    if status == 404:
        add_note(
            report,
            "ownership.owner_name",
            REASON_PRIVATE,
            "Het Kadaster-product vond bij dit adres geen kadastraal object.",
        )
        return
    if status == 409:
        add_note(
            report,
            "ownership.owner_name",
            REASON_API,
            "Kadaster vond meerdere objecten bij dit adres en levert dan geen eigenaar. Verfijn het adres met huisletter of toevoeging.",
        )
        return
    add_note(
        report,
        "ownership.owner_name",
        REASON_API,
        f"Kadaster antwoordde met HTTP {status}. {detail}".strip(),
    )


def missing_kadaster_key(report: dict) -> None:
    add_note(
        report,
        "ownership.owner_name",
        REASON_AUTH,
        "Zet KADASTER_API_KEY. Zonder sleutel is de tenaamstelling niet op te vragen.",
    )
    add_note(
        report,
        "ownership.owner_name",
        REASON_PAID,
        "De open PDOK-kadastrale kaart bevat geen eigenaar. "
        "Objectinformatie product 'rechten' kost € 2,96 per adres via Mijn Kadaster met Kadata Internet "
        "(header X-API-KEY, KADASTER_PRODUCT=objectinformatie). "
        "Contractloos kost € 0,82 per adres en is bedoeld voor nutsbedrijven (KADASTER_PRODUCT=contractloos). "
        "BRK Bevragen is alleen voor BSN-gerechtigde overheden met API-sleutel en doorgaans een PKIoverheid-certificaat "
        "(KADASTER_PRODUCT=brk, optioneel KADASTER_CERT_FILE en KADASTER_KEY_FILE).",
    )
    add_note(
        report,
        "contact_information.email",
        REASON_AUTH,
        "Zonder eigenaar is niet te bepalen welke organisatie moet worden gezocht.",
    )


def ssl_context_for_brk() -> ssl.SSLContext | None:
    cert = env("KADASTER_CERT_FILE")
    key = env("KADASTER_KEY_FILE")
    if not cert and not key:
        return None
    context = ssl.create_default_context()
    context.load_cert_chain(cert, key or None)
    return context


def pick_principal(holders: list[dict]) -> dict | None:
    if not holders:
        return None
    owners = [item for item in holders if item.get("eigendom")]
    pool = owners or holders
    pool.sort(key=lambda item: (item.get("rank") if item.get("rank") is not None else 99, 0 if item.get("type") != "private_individual" else 1))
    return pool[0]


def holder(
    *,
    name: str,
    owner_type: str,
    right: str,
    rank: int | None,
    eigendom: bool,
    kvk: str = "",
    rsin: str = "",
    address: str = "",
    source: str,
) -> dict:
    return {
        "name": norm_space(name),
        "type": owner_type,
        "right": right,
        "rank": rank,
        "eigendom": eigendom,
        "kvk": kvk if re.fullmatch(r"\d{8}", kvk or "") else "",
        "rsin": rsin if re.fullmatch(r"\d{9}", rsin or "") else "",
        "address": address,
        "source": source,
    }


def objectinformatie_holders(payload: dict) -> list[dict]:
    block = find_key(payload, "rechten")
    rows = []
    if isinstance(block, dict):
        rows = block.get("rechten") or []
    elif isinstance(block, list):
        rows = block
    found = []
    for recht in rows:
        if not isinstance(recht, dict):
            continue
        label = str(recht.get("omschrijving") or "")
        share = str(recht.get("aandeelInRecht") or "")
        right = norm_space(f"{label} {share}")
        eigendom = "eigendom" in label.lower()
        for person in recht.get("persons") or []:
            if not isinstance(person, dict):
                continue
            form = waarde(person.get("rechtsVorm") or person.get("rechtsvorm"))
            given = norm_space(str(person.get("voornamen") or ""))
            prefix = norm_space(str(person.get("naamVoorvoegsel") or person.get("voorvoegsel") or ""))
            last = norm_space(str(person.get("naam") or ""))
            natural = bool(given or prefix or person.get("geboorteDatum") or person.get("geslacht")) and not form
            display = norm_space(" ".join(part for part in (given, prefix, last) if part))
            party_address = ""
            if not natural and isinstance(person.get("address"), dict):
                party_address = format_address(person["address"])
            found.append(
                holder(
                    name=display,
                    owner_type=classify(display, form, natural_person=natural, from_entity=bool(form)),
                    right=right,
                    rank=None,
                    eigendom=eigendom,
                    kvk=digits_only(str(person.get("kvkNummer") or "")),
                    address=party_address,
                    source="kadaster-objectinformatie",
                )
            )
        for entity in recht.get("entities") or []:
            if not isinstance(entity, dict):
                continue
            form = waarde(entity.get("rechtsVorm") or entity.get("rechtsvorm"))
            name = norm_space(
                str(entity.get("statutaireNaam") or entity.get("naam") or entity.get("name") or "")
            )
            party_address = format_address(entity.get("address") or {}) if isinstance(entity.get("address"), dict) else ""
            found.append(
                holder(
                    name=name,
                    owner_type=classify(name, form, natural_person=False, from_entity=True),
                    right=right,
                    rank=None,
                    eigendom=eigendom,
                    kvk=digits_only(str(entity.get("kvkNummer") or entity.get("kvk") or "")),
                    rsin=digits_only(str(entity.get("rsin") or "")),
                    address=party_address,
                    source="kadaster-objectinformatie",
                )
            )
    return [item for item in found if item["name"]]


def find_key(payload, key: str):
    if isinstance(payload, dict):
        if key in payload and payload[key]:
            return payload[key]
        for value in payload.values():
            found = find_key(value, key)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_key(item, key)
            if found:
                return found
    return None


def kadaster_endpoint(product: str) -> str:
    override = env("KADASTER_API_BASE").rstrip("/")
    if product == "objectinformatie":
        if not override:
            return OBJECTINFORMATIE_URL
        if override.endswith("/report"):
            return override
        return override + "/objectinformatieapi/api/v1/report"
    if product == "contractloos":
        return override or CONTRACTLOOS_BASE
    return override or BRK_BASE


def fetch_objectinformatie(doc: dict, report: dict) -> list[dict]:
    secret = env("KADASTER_API_KEY")
    address = postcode_huis(doc)
    body: dict = {
        "selection": [{"code": "rechten", "deliver": "PartialProduct"}],
        "includePdf": False,
    }
    if address["postcode"] and address["number"]:
        pht = {"postalcode": address["postcode"], "houseNumber": address["number"]}
        if address["letter"]:
            pht["houseLetter"] = address["letter"]
        if address["suffix"]:
            pht["houseNumberAddition"] = address["suffix"]
        body["pht"] = pht
    elif re.fullmatch(r"\d{16}", address["nummeraanduiding"]):
        body["bagId"] = address["nummeraanduiding"]
    else:
        add_note(
            report,
            "ownership.owner_name",
            REASON_API,
            "Objectinformatie heeft een postcode met huisnummer of een BAG-nummeraanduiding van 16 cijfers nodig.",
        )
        return []
    status, payload, message = request_json(
        kadaster_endpoint("objectinformatie"),
        method="POST",
        headers={"X-API-KEY": secret},
        body=body,
        timeout=25,
    )
    if status != 200 or not isinstance(payload, dict):
        kadaster_failure(report, status, message, secret)
        return []
    return objectinformatie_holders(payload)


def contractloos_holders(payload: dict) -> list[dict]:
    found = []
    for row in payload.get("tenaamstellingen") or []:
        if not isinstance(row, dict):
            continue
        given = norm_space(str(row.get("voornamen") or ""))
        prefix = norm_space(str(row.get("voorvoegsel") or ""))
        last = norm_space(str(row.get("naam") or ""))
        natural = bool(given or prefix or row.get("geslacht") or row.get("overlijdensDatum"))
        display = norm_space(" ".join(part for part in (given, prefix, last) if part))
        right = str(row.get("aardZakelijkRecht") or "")
        party_address = ""
        addresses = row.get("adressen") or []
        if not natural and addresses and isinstance(addresses[0], dict):
            party_address = format_address(addresses[0])
        found.append(
            holder(
                name=display,
                owner_type=classify(display, "", natural_person=natural, from_entity=False),
                right=right,
                rank=row.get("rankBelangrijksteEigenaar"),
                eigendom="eigendom" in right.lower(),
                address=party_address,
                source="kadaster-contractloos",
            )
        )
    return [item for item in found if item["name"]]


def fetch_contractloos(doc: dict, parcel: dict | None, report: dict) -> list[dict]:
    secret = env("KADASTER_API_KEY")
    address = postcode_huis(doc)
    base = kadaster_endpoint("contractloos").rstrip("/")
    params: list[tuple[str, str]] = []
    path = ""
    if address["postcode"] and address["number"]:
        path = "/api/v1/postcode"
        params = [("Postcode", address["postcode"]), ("Huisnummer", address["number"])]
        if address["letter"]:
            params.append(("Huisletter", address["letter"]))
        if address["suffix"]:
            params.append(("Huisnummertoevoeging", address["suffix"]))
    elif re.fullmatch(r"\d{14,16}", address["verblijfsobject"]):
        path = "/api/v1/bag"
        params = [("Verblijfsobject_id", address["verblijfsobject"])]
    elif parcel and parcel.get("gemeente") and parcel.get("sectie") and parcel.get("nummer"):
        path = "/api/v1/kadastraleaanduiding"
        params = [
            ("KadastraleGemeente", parcel["gemeente"]),
            ("Sectie", parcel["sectie"]),
            ("Perceelnummer", parcel["nummer"]),
        ]
    else:
        add_note(
            report,
            "ownership.owner_name",
            REASON_API,
            "Contractloos heeft een postcode met huisnummer, een BAG-verblijfsobject of een kadastrale aanduiding met gemeentenaam nodig.",
        )
        return []
    url = base + path + "?" + urllib.parse.urlencode(params)
    status, payload, message = request_json(url, headers={"X-API-KEY": secret}, timeout=25)
    if status != 200 or not isinstance(payload, dict):
        kadaster_failure(report, status, message, secret)
        return []
    return contractloos_holders(payload)


def embedded_list(payload: dict, suffix: str) -> list:
    embedded = payload.get("_embedded") or {}
    if isinstance(embedded, dict):
        for key, value in embedded.items():
            if key.lower().endswith(suffix) and isinstance(value, list):
                return value
    return []


def brk_person_detail(base: str, secret: str, context: ssl.SSLContext | None, person: dict) -> dict:
    kind = str(person.get("type") or "")
    identifier = str(person.get("identificatie") or "")
    if "niet_natuurlijk" not in kind or not identifier:
        return {}
    if "ingeschreven" in kind:
        return {"rsin": identifier}
    status, payload, _message = request_json(
        f"{base}/kadasternietnatuurlijkpersonen/{urllib.parse.quote(identifier)}",
        headers={"x-api-key": secret, "Accept-Crs": "epsg:28992"},
        timeout=25,
        context=context,
    )
    if status != 200 or not isinstance(payload, dict):
        return {}
    return payload


def fetch_brk(doc: dict, parcel: dict | None, report: dict) -> list[dict]:
    secret = env("KADASTER_API_KEY")
    address = postcode_huis(doc)
    base = kadaster_endpoint("brk").rstrip("/")
    try:
        context = ssl_context_for_brk()
    except (OSError, ssl.SSLError):
        add_note(
            report,
            "ownership.owner_name",
            REASON_AUTH,
            "KADASTER_CERT_FILE of KADASTER_KEY_FILE kon niet als PKIoverheid-certificaat worden geladen.",
        )
        return []
    params: dict[str, str] = {"expand": "zakelijkGerechtigden"}
    if address["postcode"] and address["number"]:
        params.update({"postcode": address["postcode"], "huisnummer": address["number"]})
        if address["letter"]:
            params["huisletter"] = address["letter"]
        if address["suffix"]:
            params["huisnummertoevoeging"] = address["suffix"]
    elif parcel and parcel.get("gemeente") and parcel.get("sectie") and parcel.get("nummer"):
        params = {
            "kadastraleAanduiding": f"{parcel['gemeente']} {parcel['sectie']} {parcel['nummer']}",
            "expand": "zakelijkGerechtigden",
        }
    else:
        add_note(report, "ownership.owner_name", REASON_API, "BRK Bevragen heeft een postcode met huisnummer of een kadastrale aanduiding nodig.")
        return []
    status, payload, message = request_json(
        base + "/kadastraalonroerendezaken?" + urllib.parse.urlencode(params),
        headers={"x-api-key": secret},
        timeout=25,
        context=context,
    )
    if status != 200 or not isinstance(payload, dict):
        kadaster_failure(report, status, message, secret)
        return []
    zaken = embedded_list(payload, "zaken") or embedded_list(payload, "zaak")
    if not zaken and payload.get("identificatie"):
        zaken = [payload]
    if not zaken:
        add_note(report, "ownership.owner_name", REASON_PRIVATE, "BRK Bevragen vond geen kadastraal object bij dit adres.")
        return []
    zaak = zaken[0]
    gerechtigden = embedded_list(zaak, "gerechtigden")
    if not gerechtigden:
        identificatie = str(zaak.get("identificatie") or "")
        if identificatie:
            status, follow, message = request_json(
                f"{base}/kadastraalonroerendezaken/{urllib.parse.quote(identificatie)}/zakelijkgerechtigden",
                headers={"x-api-key": secret},
                timeout=25,
                context=context,
            )
            if status == 200 and isinstance(follow, dict):
                gerechtigden = embedded_list(follow, "gerechtigden")
            elif status != 200:
                kadaster_failure(report, status, message, secret)
    found = []
    for item in gerechtigden:
        if not isinstance(item, dict):
            continue
        person = item.get("persoon") or {}
        if not isinstance(person, dict):
            continue
        kind = str(person.get("type") or "")
        natural = "natuurlijk_persoon" in kind and "niet_natuurlijk" not in kind
        detail = {} if natural else brk_person_detail(base, secret, context, person)
        form = waarde(detail.get("rechtsvorm"))
        name = norm_space(str(detail.get("statutaireNaam") or person.get("omschrijving") or ""))
        kvk = digits_only(str(detail.get("kvkNummer") or ""))
        rsin = digits_only(str(detail.get("rsin") or ""))
        if not rsin and "ingeschreven_niet_natuurlijk" in kind:
            rsin = digits_only(str(person.get("identificatie") or ""))
        right = str(item.get("type") or "")
        found.append(
            holder(
                name=name,
                owner_type=classify(name, form, natural_person=natural, from_entity=not natural),
                right=right,
                rank=0 if right == "eigenaar" else 1,
                eigendom=right == "eigenaar",
                kvk=kvk,
                rsin=rsin,
                source="kadaster-brk",
            )
        )
    return [item for item in found if item["name"]]


def fetch_owner(doc: dict, parcel: dict | None, report: dict) -> dict | None:
    if not env("KADASTER_API_KEY"):
        missing_kadaster_key(report)
        return None
    product = (env("KADASTER_PRODUCT") or "objectinformatie").lower()
    try:
        if product == "contractloos":
            holders = fetch_contractloos(doc, parcel, report)
        elif product == "brk":
            holders = fetch_brk(doc, parcel, report)
        elif product == "objectinformatie":
            holders = fetch_objectinformatie(doc, report)
        else:
            add_note(
                report,
                "ownership.owner_name",
                REASON_API,
                "KADASTER_PRODUCT moet objectinformatie, contractloos of brk zijn.",
            )
            return None
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        add_note(report, "ownership.owner_name", REASON_API, f"Het Kadaster was niet bereikbaar ({exc.__class__.__name__}).")
        return None
    chosen = pick_principal(holders)
    if not chosen:
        if not any(item["field"] == "ownership.owner_name" for item in report["unavailable"]):
            add_note(
                report,
                "ownership.owner_name",
                REASON_API,
                "Het Kadaster-product gaf geen tenaamstelling terug. E-mail en telefoon zitten niet in dit product.",
            )
        return None
    extra = [item for item in holders if item is not chosen and item.get("eigendom")]
    if extra:
        add_note(
            report,
            "ownership.owner_name",
            REASON_API,
            f"Er zijn {len(extra)} extra gerechtigden. Alleen de belangrijkste eigenaar wordt getoond.",
        )
    return chosen


def kvk_headers() -> dict:
    return {"apikey": env("KVK_API_KEY")}


def kvk_get(path: str, params: dict) -> tuple[int, dict | list | None, str]:
    base = (env("KVK_API_BASE") or KVK_BASE).rstrip("/")
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    return request_json(url, headers=kvk_headers(), timeout=20)


def kvk_results(payload) -> list[dict]:
    if isinstance(payload, dict):
        rows = payload.get("resultaten") or payload.get("data") or []
        return [row for row in rows if isinstance(row, dict)]
    return []


def score_kvk(row: dict, wanted: str, city: str) -> int:
    name = fold(str(row.get("naam") or row.get("statutaireNaam") or ""))
    target = fold(wanted)
    if not name or not target:
        return 0
    score = 0
    if name == target:
        score = 100
    elif name.startswith(target) or target.startswith(name):
        shorter = min(len(name), len(target))
        longer = max(len(name), len(target))
        if shorter / longer >= 0.72:
            score = 80
    place = ""
    address = row.get("adres") or {}
    if isinstance(address, dict):
        inner = address.get("binnenlandsAdres") or address
        if isinstance(inner, dict):
            place = str(inner.get("plaats") or "")
    if city and fold(place) == fold(city):
        score += 10
    if str(row.get("actief") or "").lower() in ("nee", "false"):
        score -= 30
    return score


def choose_kvk(rows: list[dict], wanted: str, city: str) -> dict | None:
    ranked = sorted(((score_kvk(row, wanted, city), row) for row in rows), key=lambda item: item[0], reverse=True)
    if not ranked or ranked[0][0] < 80:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 10 and ranked[0][0] < 100:
        return None
    return ranked[0][1]


def collect_websites(payload) -> list[str]:
    found = []

    def visit(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in ("websites", "website") or "website" in key.lower():
                    for item in value if isinstance(value, list) else [value]:
                        if isinstance(item, str):
                            found.append(item)
                        elif isinstance(item, dict):
                            found.append(str(item.get("url") or item.get("website") or ""))
                else:
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)
    urls = []
    for item in found:
        url = normalise_website(item)
        if url and url not in urls:
            urls.append(url)
    return urls


def kvk_address(payload: dict) -> tuple[str, str]:
    addresses = []

    def visit(node) -> None:
        if isinstance(node, dict):
            if node.get("straatnaam") or node.get("postcode"):
                addresses.append(node)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)
    preferred = [item for item in addresses if "bezoek" in str(item.get("type") or "").lower()]
    chosen = (preferred or addresses or [None])[0]
    if not chosen:
        return "", ""
    return format_address(chosen), str(chosen.get("plaats") or "")


def kvk_rechtsvorm(payload: dict) -> str:
    embedded = payload.get("_embedded") or {}
    owner = embedded.get("eigenaar") or {}
    if isinstance(owner, dict):
        return str(owner.get("uitgebreideRechtsvorm") or owner.get("rechtsvorm") or "")
    return ""


def lookup_kvk(name: str, city: str, kvk: str, rsin: str, report: dict) -> dict | None:
    if not env("KVK_API_KEY"):
        add_note(
            report,
            "ownership.kvk_number",
            REASON_AUTH,
            "Zet KVK_API_KEY. De sleutel komt uit developers.kvk.nl en gaat mee als header apikey.",
        )
        add_note(
            report,
            "ownership.kvk_number",
            REASON_PAID,
            "KVK Zoeken en Basisprofiel zijn aparte producten. Basisprofiel is betaald en levert de statutaire naam, het bezoekadres en geregistreerde websites. Het handelsregister levert geen e-mail en geen telefoon.",
        )
        return None
    try:
        if re.fullmatch(r"\d{8}", kvk or ""):
            status, payload, message = kvk_get(f"/api/v1/basisprofielen/{kvk}", {})
        else:
            params = {"resultatenPerPagina": "10", "pagina": "1"}
            if re.fullmatch(r"\d{9}", rsin or ""):
                params["rsin"] = rsin
            else:
                params["naam"] = name[:100]
                if city:
                    params["plaats"] = city[:100]
            status, payload, message = kvk_get("/api/v2/zoeken", params)
            if status != 200 or not isinstance(payload, dict):
                add_note(report, "ownership.kvk_number", REASON_AUTH if status in (401, 403) else REASON_API, f"KVK Zoeken antwoordde met HTTP {status}. {redact(message, env('KVK_API_KEY'))}")
                return None
            chosen = choose_kvk(kvk_results(payload), name, city)
            if not chosen:
                add_note(
                    report,
                    "ownership.kvk_number",
                    REASON_NO_MATCH,
                    f"KVK Zoeken gaf geen eenduidige inschrijving voor '{name}'.",
                )
                return None
            number = digits_only(str(chosen.get("kvkNummer") or ""))
            status, payload, message = kvk_get(f"/api/v1/basisprofielen/{number}", {})
        if status in (401, 403):
            add_note(report, "ownership.kvk_number", REASON_AUTH, f"KVK antwoordde met HTTP {status}. {redact(message, env('KVK_API_KEY'))}")
            return None
        if status != 200 or not isinstance(payload, dict):
            add_note(report, "ownership.kvk_number", REASON_API, f"KVK Basisprofiel antwoordde met HTTP {status}. {redact(message, env('KVK_API_KEY'))}")
            return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        add_note(report, "ownership.kvk_number", REASON_API, f"KVK was niet bereikbaar ({exc.__class__.__name__}).")
        return None
    number = digits_only(str(payload.get("kvkNummer") or kvk))
    official = norm_space(str(payload.get("statutaireNaam") or payload.get("naam") or name))
    address, place = kvk_address(payload)
    return {
        "kvk": number,
        "name": official,
        "rechtsvorm": kvk_rechtsvorm(payload),
        "address": address,
        "city": place,
        "websites": collect_websites(payload),
    }


def wikidata_search(name: str) -> list[str]:
    params = {
        "action": "wbsearchentities",
        "search": name,
        "language": "nl",
        "uselang": "nl",
        "format": "json",
        "type": "item",
        "limit": "5",
    }
    status, payload, _message = request_json(WIKIDATA_API + "?" + urllib.parse.urlencode(params), timeout=15)
    if status != 200 or not isinstance(payload, dict):
        return []
    return [item.get("id") for item in payload.get("search") or [] if item.get("id")]


def claim_values(entity: dict, prop: str) -> list[str]:
    values = []
    for claim in (entity.get("claims") or {}).get(prop) or []:
        datavalue = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(datavalue, str):
            values.append(datavalue)
        elif isinstance(datavalue, dict) and isinstance(datavalue.get("text"), str):
            values.append(datavalue["text"])
    return values


def entity_labels(entity: dict) -> list[str]:
    labels = []
    for bucket in (entity.get("labels") or {}, entity.get("aliases") or {}):
        for lang in ("nl", "en"):
            value = bucket.get(lang)
            rows = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
            for row in rows:
                if isinstance(row, dict) and row.get("value"):
                    labels.append(row["value"])
    return labels


def entity_description(entity: dict) -> str:
    descriptions = entity.get("descriptions") or {}
    for lang in ("nl", "en"):
        value = descriptions.get(lang) or {}
        if isinstance(value, dict) and value.get("value"):
            return value["value"]
    return ""


def lookup_wikidata(name: str, owner_type: str) -> dict | None:
    queries = [name]
    stripped = re.sub(r"^(gemeente|provincie|waterschap|stichting|vereniging)\s+", "", name, flags=re.I).strip()
    if stripped and stripped.lower() != name.lower():
        queries.append(stripped)
    ids: list[str] = []
    for query in queries:
        for item_id in wikidata_search(query):
            if item_id not in ids:
                ids.append(item_id)
        if len(ids) >= 6:
            break
    if not ids:
        return None
    params = {
        "action": "wbgetentities",
        "ids": "|".join(ids[:6]),
        "props": "labels|aliases|descriptions|claims",
        "languages": "nl|en",
        "format": "json",
    }
    status, payload, _message = request_json(WIKIDATA_API + "?" + urllib.parse.urlencode(params), timeout=20)
    if status != 200 or not isinstance(payload, dict):
        return None
    target = fold(name)
    short = fold(stripped or name)
    best = None
    best_score = 0
    for entity in (payload.get("entities") or {}).values():
        if not isinstance(entity, dict) or entity.get("missing"):
            continue
        description = entity_description(entity).lower()
        if "wikinews" in description or "wikipedia" in description and "disambiguation" in description:
            continue
        score = 0
        labels = [fold(label) for label in entity_labels(entity)]
        if target in labels:
            score += 100
        elif owner_type in ("municipality", "government") and short in labels and any(
            word in description for word in ("gemeente", "municipality", "provincie", "waterschap", "government")
        ):
            score += 85
        if claim_values(entity, "P856"):
            score += 5
        if score > best_score:
            best_score = score
            best = entity
    if not best or best_score < 85:
        return None
    websites = [normalise_website(value) for value in claim_values(best, "P856")]
    emails = [clean_email(value.removeprefix("mailto:")) for value in claim_values(best, "P968")]
    phones = [display_phone(value) for value in claim_values(best, "P1329")]
    addresses = claim_values(best, "P6375")
    return {
        "websites": [item for item in websites if item],
        "email": next((item for item in emails if item), ""),
        "phone": next((item for item in phones if item), ""),
        "address": addresses[0] if addresses else "",
    }


class AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href") or ""
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, norm_space("".join(self._chunks))))
            self._href = ""


def normalise_website(value: str) -> str:
    text = norm_space(html_unescape(value))
    if not text or text.startswith(("mailto:", "tel:", "javascript:")):
        return ""
    if text.startswith("//"):
        text = "https:" + text
    if not re.match(r"https?://", text, re.I):
        if re.match(r"[a-z0-9.-]+\.[a-z]{2,}(/|$)", text, re.I):
            text = "https://" + text
        else:
            return ""
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return ""
    if not public_host(parsed.hostname):
        return ""
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def html_unescape(value: str) -> str:
    import html

    return html.unescape(urllib.parse.unquote(value or ""))


def public_host(hostname: str) -> bool:
    host = hostname.strip(".").lower()
    if not host or host in ("localhost",) or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            previous = socket.getdefaulttimeout()
            socket.setdefaulttimeout(5)
            try:
                infos = socket.getaddrinfo(host, None)
            finally:
                socket.setdefaulttimeout(previous)
        except (socket.gaierror, TimeoutError, OSError):
            return False
        addresses = [item[4][0] for item in infos]
        return bool(addresses) and all(public_ip(item) for item in addresses)
    return public_ip(address.exploded)


def public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def registrable(hostname: str) -> str:
    parts = hostname.lower().split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return hostname.lower()


def clean_email(value: str) -> str:
    text = html_unescape(value).strip().strip(".").lower()
    match = re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", text)
    if not match:
        return ""
    domain = text.split("@", 1)[1]
    rejected = (
        "example.com",
        "example.org",
        "sentry.io",
        "schema.org",
        "wixpress.com",
        "googleapis.com",
        "gstatic.com",
        "cloudflare.com",
        "wordpress.com",
        "gravatar.com",
        "mysite.com",
    )
    if domain in rejected or domain.endswith((".png", ".jpg", ".svg", ".webp")):
        return ""
    return text


def display_phone(value: str) -> str:
    text = html_unescape(value).strip()
    text = re.sub(r"^tel:", "", text, flags=re.I)
    digits = re.sub(r"[^\d+]", "", text)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if re.fullmatch(r"14\d{3,4}", digits):
        return "14 " + digits[2:]
    if re.fullmatch(r"\+?\d{8,15}", digits):
        return digits
    return ""


def jsonld_contacts(html_text: str) -> list[dict]:
    found = []
    for match in re.finditer(
        r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html_text,
        flags=re.I | re.S,
    ):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        walk_jsonld(payload, found)
    return found


def walk_jsonld(node, found: list[dict]) -> None:
    if isinstance(node, list):
        for item in node:
            walk_jsonld(item, found)
        return
    if not isinstance(node, dict):
        return
    kind = " ".join(node.get("@type") or []) if isinstance(node.get("@type"), list) else str(node.get("@type") or "")
    email = clean_email(str(node.get("email") or ""))
    phone = display_phone(str(node.get("telephone") or ""))
    person = ""
    if "ContactPoint" in kind:
        person = norm_space(str(node.get("name") or node.get("contactType") or ""))
    if email or phone or person:
        found.append({"email": email, "phone": phone, "person": person})
    for key in ("contactPoint", "mainEntity", "@graph", "department"):
        if key in node:
            walk_jsonld(node[key], found)


def choose_email(candidates: list[str], website: str) -> str:
    if not candidates:
        return ""
    host = registrable(urllib.parse.urlsplit(website).hostname or "") if website else ""

    def rank(email: str) -> tuple:
        local, domain = email.split("@", 1)
        same = 0 if host and (domain == host or domain.endswith("." + host)) else 1
        preferred = 0 if local.split("+")[0] in {
            "info",
            "contact",
            "gemeente",
            "receptie",
            "service",
            "klantenservice",
            "hello",
            "hallo",
            "sales",
            "verkoop",
        } else 1
        generic = 0 if not local.startswith("noreply") else 1
        return (same, generic, preferred, local)

    return sorted(set(candidates), key=rank)[0]


def fetch_html(url: str) -> str:
    if url in _PAGE_CACHE:
        return _PAGE_CACHE[url]
    if not public_host(urllib.parse.urlsplit(url).hostname or ""):
        return ""
    try:
        status, raw, final = request_bytes(
            url,
            headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": USER_AGENT},
            timeout=12,
        )
    except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError):
        _PAGE_CACHE[url] = ""
        return ""
    if status != 200:
        _PAGE_CACHE[url] = ""
        return ""
    final_host = urllib.parse.urlsplit(final).hostname or ""
    original_host = urllib.parse.urlsplit(url).hostname or ""
    if registrable(final_host) != registrable(original_host):
        _PAGE_CACHE[url] = ""
        return ""
    text = raw[:900_000].decode("utf-8", "replace")
    _PAGE_CACHE[url] = text
    return text


def contact_links(page_url: str, html_text: str) -> list[str]:
    parser = AnchorParser()
    try:
        parser.feed(html_text)
    except Exception:
        return []
    host = registrable(urllib.parse.urlsplit(page_url).hostname or "")
    found = []
    for href, text in parser.links:
        absolute = urllib.parse.urljoin(page_url, href)
        parsed = urllib.parse.urlsplit(absolute)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        if registrable(parsed.hostname) != host:
            continue
        blob = f"{parsed.path} {text}".lower()
        if not any(token in blob for token in ("contact", "klantenservice", "bereik", "over-ons", "over ons")):
            continue
        cleaned = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        if cleaned not in found and cleaned.rstrip("/") != page_url.rstrip("/"):
            found.append(cleaned)
    return found[:3]


def read_one_site(url: str) -> dict:
    emails: list[str] = []
    phones: list[str] = []
    people: list[str] = []
    html_text = fetch_html(url)
    pages = [url]
    if html_text:
        pages.extend(contact_links(url, html_text))
    for page in pages[:4]:
        body = html_text if page == url else fetch_html(page)
        if not body:
            continue
        for raw in re.findall(r"mailto:([^\"'\s>?#]+)", body, flags=re.I):
            email = clean_email(raw)
            if email:
                emails.append(email)
        for raw in re.findall(r"tel:([^\"'\s>?#]+)", body, flags=re.I):
            phone = display_phone(raw)
            if phone:
                phones.append(phone)
        for item in jsonld_contacts(body):
            if item["email"]:
                emails.append(item["email"])
            if item["phone"]:
                phones.append(item["phone"])
            if item["person"] and "@" not in item["person"]:
                people.append(item["person"])
        if emails and phones:
            break
    return {
        "website": url,
        "email": choose_email(emails, url),
        "phone": phones[0] if phones else "",
        "person": people[0] if people else "",
        "from_site": bool(emails or phones or people),
    }


def read_site(urls: list[str]) -> dict:
    empty = {"website": "", "email": "", "phone": "", "person": "", "from_site": False}
    usable = [url for url in urls if url]
    for url in usable[:2]:
        found = read_one_site(url)
        if found["from_site"]:
            return found
    if usable:
        empty["website"] = usable[0]
    return empty


def enrich_organization(owner: dict, report: dict) -> None:
    contact = report["contact_information"]
    contact["company_name"] = owner["name"]
    if owner.get("address"):
        contact["business_address"] = owner["address"]
    if owner.get("kvk"):
        report["ownership"]["kvk_number"] = owner["kvk"]
    kvk = lookup_kvk(owner["name"], owner.get("city") or "", owner.get("kvk") or "", owner.get("rsin") or "", report)
    websites: list[str] = []
    sources: list[str] = []
    if kvk:
        if kvk.get("kvk"):
            report["ownership"]["kvk_number"] = kvk["kvk"]
        if kvk.get("name"):
            contact["company_name"] = kvk["name"]
        if kvk.get("address"):
            contact["business_address"] = kvk["address"]
        if kvk.get("rechtsvorm"):
            report["ownership"]["owner_type"] = classify(
                contact["company_name"], kvk["rechtsvorm"], natural_person=False, from_entity=True
            )
        websites.extend(kvk.get("websites") or [])
        sources.append("kvk")
    try:
        wiki = lookup_wikidata(contact["company_name"] or owner["name"], report["ownership"]["owner_type"])
    except (urllib.error.URLError, TimeoutError, OSError):
        wiki = None
    if wiki:
        for url in wiki["websites"]:
            if url not in websites:
                websites.append(url)
        if not contact["business_address"] and wiki.get("address"):
            contact["business_address"] = wiki["address"]
        sources.append("wikidata")
    elif not websites:
        add_note(
            report,
            "contact_information.website",
            REASON_NO_MATCH if kvk is not None or not env("KVK_API_KEY") else REASON_PRIVATE,
            "Geen officiële website gevonden in het KVK-basisprofiel of op Wikidata.",
        )
    site = read_site(websites) if websites else {"website": "", "email": "", "phone": "", "person": "", "from_site": False}
    if site["website"]:
        contact["website"] = site["website"]
    if site["email"]:
        contact["email"] = site["email"]
    if site["phone"]:
        contact["phone"] = site["phone"]
    if site["person"]:
        contact["contact_person"] = site["person"]
    if site["from_site"]:
        sources.append("official-website")
        report["contact_verified"] = bool(contact["email"] or contact["phone"])
    elif wiki and (wiki.get("email") or wiki.get("phone")):
        contact["email"] = contact["email"] or wiki.get("email") or ""
        contact["phone"] = contact["phone"] or wiki.get("phone") or ""
        report["contact_verified"] = False
        add_note(
            report,
            "contact_information.email",
            REASON_PRIVATE,
            "E-mail of telefoon komt uit Wikidata en is niet teruggevonden op de eigen website. Daarom is contact_verified false.",
        )
    if kvk and not contact["email"]:
        add_note(
            report,
            "contact_information.email",
            REASON_API,
            "De KVK Basisprofiel-API levert geen e-mailadres.",
        )
    if kvk and not contact["phone"]:
        add_note(
            report,
            "contact_information.phone",
            REASON_API,
            "De KVK Basisprofiel-API levert geen telefoonnummer.",
        )
    if not contact["email"]:
        add_note(
            report,
            "contact_information.email",
            REASON_PRIVATE if websites else REASON_NO_MATCH,
            "Geen openbaar zakelijk e-mailadres gevonden op de officiële website."
            if websites
            else "Zonder officiële website is er geen openbaar zakelijk e-mailadres om te lezen.",
        )
    if not contact["phone"]:
        add_note(
            report,
            "contact_information.phone",
            REASON_PRIVATE if websites else REASON_NO_MATCH,
            "Geen openbaar zakelijk telefoonnummer gevonden op de officiële website."
            if websites
            else "Zonder officiële website is er geen openbaar zakelijk telefoonnummer om te lezen.",
        )
    if not contact["contact_person"]:
        add_note(
            report,
            "contact_information.contact_person",
            REASON_PRIVATE,
            "Er is geen contactpersoon of afdeling als zodanig gepubliceerd.",
        )
    report["contact_source"] = "+".join(dict.fromkeys(sources))


def private_notes(report: dict, owner_type: str) -> None:
    if owner_type == "private_individual":
        detail = (
            "De eigenaar is een natuurlijk persoon. Kadaster levert geen persoonlijk e-mailadres of telefoonnummer, "
            "en die gegevens worden niet uit andere bronnen gezocht."
        )
    else:
        detail = (
            "Het Kadaster gaf geen rechtsvorm en geen voornamen. Zoeken op alleen die naam zou een andere "
            "organisatie of een particulier kunnen raken, dus er wordt geen contact gezocht."
        )
    for field in (
        "contact_information.email",
        "contact_information.phone",
        "contact_information.website",
        "contact_information.business_address",
        "contact_information.contact_person",
    ):
        add_note(report, field, REASON_PRIVATE, detail)
    add_note(report, "contact_information.email", REASON_API, "Kadaster-producten bevatten geen e-mail en geen telefoon.")


def rapport(doc: dict) -> dict:
    report = blank()
    try:
        parcel = resolve_parcel(doc)
    except (urllib.error.URLError, TimeoutError, OSError):
        parcel = None
        add_note(report, "ownership.parcel", REASON_API, "De PDOK-kadastrale kaart was niet bereikbaar.")
    if parcel and parcel.get("designation"):
        report["ownership"]["parcel"] = parcel["designation"]
        report["ownership"]["parcel_id"] = parcel.get("id") or ""
    elif not any(item["field"] == "ownership.parcel" for item in report["unavailable"]):
        add_note(
            report,
            "ownership.parcel",
            REASON_PRIVATE,
            "PDOK Locatieserver en de kadastrale kaart gaven geen perceel bij dit adres.",
        )
    owner = fetch_owner(doc, parcel, report)
    if not owner:
        return report
    report["ownership"]["owner_name"] = owner["name"]
    report["ownership"]["owner_type"] = owner["type"]
    report["ownership"]["kvk_number"] = owner.get("kvk") or ""
    report["ownership"]["right"] = owner.get("right") or ""
    report["ownership"]["source"] = owner.get("source") or ""
    if owner["type"] in ("private_individual", "unknown"):
        private_notes(report, owner["type"])
        return report
    owner["city"] = postcode_huis(doc)["city"]
    try:
        enrich_organization(owner, report)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        add_note(report, "contact_information.email", REASON_API, f"De contactbron was niet bereikbaar ({exc.__class__.__name__}).")
    return report
