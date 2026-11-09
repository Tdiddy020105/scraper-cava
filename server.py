"""Scraper: zoek openbare plan- en vergunningpublicaties.

Bron is de officiële SRU-koppeling van Officiële Bekendmakingen
(repository.overheid.nl). Pandgegevens komen uit de open BAG.
Eigenaar en openbaar organisatiecontact komen uit contact.py.

Start:  python server.py
Open:   http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bestemming as bestemmingbron
import contact as contactbron

HOST = "127.0.0.1"
PORT = 8765
ROOT = Path(__file__).resolve().parent
SRU_URL = "https://repository.overheid.nl/sru"
LOCATIE_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/suggest"
LOCATIE_FREE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
LOCATIE_REVERSE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse"
BAG_URL = "https://api.pdok.nl/kadaster/bag/ogc/v2"
USER_AGENT = "Scraper/0.1 (lokaal hergebruik officiële bekendmakingen en BAG)"
PAGE_SIZE = 15
PROVINCIES = (
    "Drenthe",
    "Flevoland",
    "Fryslân",
    "Gelderland",
    "Groningen",
    "Limburg",
    "Noord-Brabant",
    "Noord-Holland",
    "Overijssel",
    "Utrecht",
    "Zeeland",
    "Zuid-Holland",
)
GEBRUIKSDOEL = {
    "woonfunctie": "wonen",
    "bijeenkomstfunctie": "bijeenkomst",
    "celfunctie": "cel",
    "gezondheidszorgfunctie": "zorg",
    "industriefunctie": "industrie",
    "kantoorfunctie": "kantoor",
    "logiesfunctie": "logies",
    "onderwijsfunctie": "onderwijs",
    "sportfunctie": "sport",
    "winkelfunctie": "winkel",
    "overige gebruiksfunctie": "overig",
}
_provincie_cache: dict[str, list[str]] = {}
_punt_cache: dict[str, tuple[float, float]] = {}
_pand_cache: dict[str, dict] = {}
_reverse_cache: dict[str, str] = {}

NS = {
    "sru": "http://docs.oasis-open.org/ns/search-ws/sruResponse",
    "gzd": "http://standaarden.overheid.nl/sru",
    "dc": "http://purl.org/dc/terms/",
    "ow": "http://standaarden.overheid.nl/wetgeving/",
}

RUBRIEKEN = {
    "plan": "ruimtelijk plan of omgevingsdocument",
    "vergunning": "omgevingsvergunning",
    "exploitatie": "exploitatievergunning",
    "melding": "exploitatiemelding",
}

FASES = {
    "ontwerp": "ontwerp inzage",
    "vastgesteld": "vastgesteld verleend onherroepelijk",
}


def cql_term(value: str, limit: int = 80) -> str:
    """Quote anything that is not a single word, so filters cannot change the query."""
    cleaned = re.sub(r'["\\<>]', "", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()[:limit]
    if not cleaned:
        return ""
    if re.fullmatch(r"[0-9A-Za-zÀ-ÿ]+", cleaned):
        return cleaned
    return f'"{cleaned}"'


def build_query(params: dict, extra: list[str] | None = None) -> str:
    parts = ["c.product-area==officielepublicaties"]

    rubriek = params.get("rubriek", "plan")
    if rubriek == "vroeg":
        parts.append(
            '(dt.type=="ruimtelijk plan of omgevingsdocument" '
            'OR dt.type=="omgevingsvergunning" '
            'OR dt.type=="exploitatievergunning")'
        )
    elif rubriek in RUBRIEKEN:
        parts.append(f'dt.type=="{RUBRIEKEN[rubriek]}"')

    gemeente = cql_term(params.get("gemeente", ""), 60)
    if gemeente:
        parts.append(f"(dt.creator={gemeente} OR w.gemeentenaam={gemeente})")

    woorden = cql_term(params.get("q", ""), 80)
    if woorden:
        parts.append(f"cql.textAndIndexes all {woorden}")

    fase = params.get("fase", "")
    if fase in FASES:
        parts.append(f"cql.textAndIndexes any {cql_term(FASES[fase])}")

    vanaf = params.get("vanaf", "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", vanaf or ""):
        parts.append(f"dt.available>={vanaf}")

    parts.extend(extra or [])
    return " AND ".join(parts) + " sortBy dt.available/sort.descending"


def locatie_docs(params: list[tuple[str, str]]) -> list[dict]:
    url = LOCATIE_FREE + "?" + urllib.parse.urlencode(params)
    raw = fetch(url, accept="application/json")
    data = json.loads(raw.decode("utf-8"))
    return data.get("response", {}).get("docs", [])


def gemeenten_van_provincie(provincie: str) -> list[str]:
    if provincie in _provincie_cache:
        return _provincie_cache[provincie]
    docs = locatie_docs(
        [
            ("q", "*"),
            ("fq", "type:gemeente"),
            ("fq", f"provincienaam:{provincie}"),
            ("rows", "80"),
            ("fl", "gemeentenaam"),
            ("sort", "gemeentenaam asc"),
        ]
    )
    names = []
    for doc in docs:
        name = (doc.get("gemeentenaam") or "").strip()
        if name and name not in names:
            names.append(name)
    _provincie_cache[provincie] = names
    return names


def parse_punt(value: str) -> tuple[float, float] | None:
    match = re.search(r"POINT\(([0-9.]+)\s+([0-9.]+)\)", value or "")
    if not match:
        return None
    lon, lat = float(match.group(1)), float(match.group(2))
    return lat, lon


def geocode(plaats: str) -> tuple[float, float] | None:
    key = plaats.casefold()
    if key in _punt_cache:
        return _punt_cache[key]
    for soort in ("woonplaats", "gemeente"):
        docs = locatie_docs(
            [("q", plaats), ("fq", f"type:{soort}"), ("rows", "1"), ("fl", "centroide_ll")]
        )
        if not docs:
            continue
        punt = parse_punt(docs[0].get("centroide_ll", ""))
        if punt:
            _punt_cache[key] = punt
            return punt
    return None


def region_parts(params: dict) -> tuple[list[str], str | None]:
    extra: list[str] = []
    provincie = (params.get("provincie") or "").strip()
    if provincie:
        if provincie not in PROVINCIES:
            return [], "Kies een provincie uit de lijst."
        namen = gemeenten_van_provincie(provincie)
        if not namen:
            return [], "Voor die provincie zijn geen gemeenten gevonden."
        termen = " OR ".join(f"dt.creator={cql_term(naam, 40)}" for naam in namen)
        extra.append(f"({termen})")

    plaats = re.sub(r"\s+", " ", params.get("plaats") or "").strip()[:60]
    if plaats:
        ruw = params.get("straal") or "5"
        if not re.fullmatch(r"\d{1,2}", ruw) or not 1 <= int(ruw) <= 25:
            return [], "Kies een straal tussen 1 en 25 km."
        punt = geocode(plaats)
        if not punt:
            return [], "Die plaats is niet gevonden."
        lat, lon = punt
        extra.append(f'w.locatiepunt within/etrs89 "{lat:.5f} {lon:.5f} {int(ruw):.3f}"')
    return extra, None


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def texts(parent: ET.Element, name: str) -> list[str]:
    found = []
    for node in parent.iter():
        if local(node.tag) == name and node.text and node.text.strip():
            value = re.sub(r"\s+", " ", node.text).strip()
            if value not in found:
                found.append(value)
    return found


def first(parent: ET.Element, name: str) -> str:
    values = texts(parent, name)
    return values[0] if values else ""


TITLE_ADDRESS = re.compile(
    r"([A-ZÀ-Ý][\w'.\-]*(?:\s+(?:[A-ZÀ-Ý][\w'.\-]*|van|de|den|der|het|ten|ter|op|aan)){0,6})\s+"
    r"(\d{1,4}[A-Za-z]?(?:-\d{1,4})?)\s*,\s*"
    r"(\d{4}\s?[A-Z]{2})"
    r"(?:\s+((?:['’]s-)?[A-ZÀ-Ý][\w'.\-]+))?"
)


def format_postcode(value: str) -> str:
    compact = value.replace(" ", "")
    if len(compact) == 6:
        return compact[:4] + " " + compact[4:]
    return value


def address_from_title(title: str) -> str:
    match = TITLE_ADDRESS.search(title)
    if not match:
        return ""
    street, number, postcode, place = match.groups()
    tail = " ".join(part for part in (format_postcode(postcode), place or "") if part)
    return f"{street} {number}, {tail}"


def address(record: ET.Element, title: str) -> str:
    street = first(record, "straatnaam")
    number = first(record, "huisnummer")
    letter = first(record, "huisletter")
    extra = first(record, "huisnummertoevoeging")
    postcode = first(record, "postcode")
    place = first(record, "woonplaats")
    line = " ".join(part for part in (street, f"{number}{letter}{extra}".strip()) if part)
    if line:
        tail = " ".join(part for part in (format_postcode(postcode) if postcode else "", place) if part)
        return ", ".join(part for part in (line, tail) if part)
    return address_from_title(title)


def coordinaten(record: ET.Element) -> tuple[float, float] | None:
    for value in texts(record, "locatiepunt"):
        parts = re.split(r"[\s,]+", value.strip())
        if len(parts) < 2:
            continue
        try:
            breedte, lengte = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if 50 <= breedte <= 54 and 3 <= lengte <= 8:
            return breedte, lengte
    return None


def dichtstbij(lat: float, lon: float) -> str:
    key = f"{lat:.5f},{lon:.5f}"
    if key in _reverse_cache:
        return _reverse_cache[key]
    params = urllib.parse.urlencode(
        {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "type": "adres", "rows": "1"}
    )
    raw = fetch(LOCATIE_REVERSE + "?" + params, accept="application/json")
    docs = json.loads(raw.decode("utf-8")).get("response", {}).get("docs", [])
    naam = (docs[0].get("weergavenaam") if docs else "") or ""
    if len(_reverse_cache) > 400:
        _reverse_cache.clear()
    _reverse_cache[key] = naam
    return naam


def vul_adressen(items: list[dict]) -> None:
    openstaand = [item for item in items if not item["adres"] and item.get("lat") is not None]
    if not openstaand:
        return
    with ThreadPoolExecutor(max_workers=6) as pool:
        taken = {pool.submit(dichtstbij, item["lat"], item["lon"]): item for item in openstaand}
        for klaar in as_completed(taken):
            item = taken[klaar]
            try:
                gevonden = klaar.result()
            except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError):
                gevonden = ""
            if gevonden:
                item["adres"] = gevonden
                item["adresBron"] = "pdok"


def plan_url(plan_id: str) -> str:
    if not plan_id:
        return ""
    return (
        "https://omgevingswet.overheid.nl/regels-op-de-kaart/document?documentID="
        + urllib.parse.quote(plan_id, safe="")
    )


def parse_records(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    diagnostics = []
    for node in root.iter():
        if local(node.tag) == "message" and node.text:
            diagnostics.append(node.text.strip())

    total_text = ""
    for node in root.iter():
        if local(node.tag) == "numberOfRecords" and node.text:
            total_text = node.text.strip()
            break

    items = []
    for record in root.iter():
        if local(record.tag) != "recordData":
            continue
        title = first(record, "title")
        if not title:
            continue
        identifier = first(record, "identifier")
        preferred = first(record, "preferredUrl")
        if not preferred and identifier:
            preferred = f"https://zoek.officielebekendmakingen.nl/{identifier}.html"
        plan_id = first(record, "bekendmakingBetreffendePlan")
        punt = coordinaten(record)
        items.append(
            {
                "id": identifier,
                "titel": title,
                "type": first(record, "type"),
                "gemeente": first(record, "creator"),
                "datum": first(record, "available") or first(record, "date"),
                "gewijzigd": first(record, "modified"),
                "reactieTot": first(record, "datumEindeReactietermijn"),
                "adres": address(record, title),
                "adresBron": "",
                "lat": punt[0] if punt else None,
                "lon": punt[1] if punt else None,
                "planId": plan_id,
                "planUrl": plan_url(plan_id),
                "url": preferred,
                "blad": first(record, "publicatienaam"),
            }
        )

    try:
        total = int(total_text)
    except ValueError:
        total = len(items)
    return {"totaal": total, "resultaten": items, "meldingen": diagnostics}


def fetch(url: str, accept: str = "application/xml") -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read()


def search(params: dict) -> dict:
    try:
        page = max(1, int(params.get("page", "1")))
    except ValueError:
        page = 1
    try:
        extra, region_fout = region_parts(params)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return {"fout": "De plaats of provincie kon niet worden opgezocht.", "query": ""}
    if region_fout:
        return {"fout": region_fout, "query": ""}
    query = build_query(params, extra)
    sru_params = {
        "query": query,
        "startRecord": str((page - 1) * PAGE_SIZE + 1),
        "maximumRecords": str(PAGE_SIZE),
        "httpAccept": "application/xml",
    }
    url = SRU_URL + "?" + urllib.parse.urlencode(sru_params)
    try:
        payload = fetch(url)
    except urllib.error.HTTPError as exc:
        return {"fout": f"De zoekdienst antwoordde met {exc.code}.", "query": query}
    except urllib.error.URLError:
        return {"fout": "Officiële Bekendmakingen is nu niet bereikbaar.", "query": query}

    parsed = parse_records(payload)
    vul_adressen(parsed["resultaten"])
    alleen_adres = params.get("adres") == "1"
    resultaten = parsed["resultaten"]
    if alleen_adres:
        resultaten = [item for item in resultaten if item["adres"]]
    return {
        "totaal": parsed["totaal"],
        "pagina": page,
        "perPagina": PAGE_SIZE,
        "resultaten": resultaten,
        "query": query,
        "meldingen": parsed["meldingen"],
        "adresFilter": alleen_adres,
    }


def json_props(payload: dict) -> dict:
    if payload.get("properties"):
        return payload["properties"]
    features = payload.get("features") or []
    if features:
        return features[0].get("properties") or {}
    return {}


def kies_adres(docs: list[dict], vraag: str) -> dict:
    match = re.search(r"(\d+)\s*([A-Za-z])?", vraag)
    if not match:
        return docs[0]
    nummer, letter = match.group(1), (match.group(2) or "").upper()

    def score(doc: dict) -> int:
        huis = str(doc.get("huisnummer") or "")
        extra = str(doc.get("huisletter") or "").upper()
        punten = 5 if huis == nummer else 0
        if letter:
            punten += 3 if extra == letter else -2
        elif not extra:
            punten += 3
        return punten

    return max(docs, key=score)


def kies_verblijfsobject(features: list[dict]) -> dict:
    def rang(feature: dict) -> int:
        status = ((feature.get("properties") or {}).get("status") or "").lower()
        return 0 if "in gebruik" in status else 1

    return sorted(features, key=rang)[0]


def pand(adres: str) -> dict:
    cleaned = re.sub(r"\s+", " ", adres or "").strip()[:120]
    if len(cleaned) < 5:
        return {"fout": "Geen bruikbaar adres."}
    if cleaned in _pand_cache:
        return _pand_cache[cleaned]
    try:
        docs = locatie_docs([("q", cleaned), ("fq", "type:adres"), ("rows", "8")])
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return {"fout": "Het adres kon niet worden opgezocht."}
    if not docs:
        return {"fout": "Geen BAG-adres gevonden."}
    doc = kies_adres(docs, cleaned)
    identificatie = str(doc.get("adresseerbaarobject_id") or "")
    if not re.fullmatch(r"\d{14,16}", identificatie):
        return {"fout": "Dit adres heeft geen verblijfsobject."}
    try:
        raw = fetch(
            f"{BAG_URL}/collections/verblijfsobject/items?f=json&identificatie={identificatie}&limit=5",
            accept="application/json",
        )
        features = json.loads(raw.decode("utf-8")).get("features") or []
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        features = []
    props = kies_verblijfsobject(features).get("properties", {}) if features else {}
    ruwe_doelen = props.get("gebruiksdoel") or ""
    if isinstance(ruwe_doelen, list):
        doelen = [str(item) for item in ruwe_doelen]
    else:
        doelen = [item.strip() for item in str(ruwe_doelen).split(",") if item.strip()]
    bouwjaar = None
    hrefs = props.get("pand.href") or []
    if isinstance(hrefs, str):
        hrefs = [hrefs]
    if hrefs and str(hrefs[0]).startswith(f"{BAG_URL}/"):
        try:
            scheiding = "&" if "?" in hrefs[0] else "?"
            pand_raw = fetch(hrefs[0] + scheiding + "f=json", accept="application/json")
            bouwjaar = json_props(json.loads(pand_raw.decode("utf-8"))).get("bouwjaar")
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
            bouwjaar = None
    punt = parse_punt(doc.get("centroide_ll", ""))
    perceel = doc.get("gekoppeld_perceel") or ""
    if isinstance(perceel, list):
        perceel = ", ".join(str(item) for item in perceel[:3])
    resultaat = {
        "adres": doc.get("weergavenaam") or cleaned,
        "lat": punt[0] if punt else None,
        "lon": punt[1] if punt else None,
        "oppervlakte": props.get("oppervlakte"),
        "gebruiksdoelen": [GEBRUIKSDOEL.get(item, item) for item in doelen],
        "bouwjaar": bouwjaar,
        "perceel": perceel,
        "gemeente": doc.get("gemeentenaam") or "",
        "woonplaats": doc.get("woonplaatsnaam") or "",
    }
    try:
        resultaat.update(contactbron.rapport(doc))
    except Exception:
        resultaat.update(
            contactbron.failed(
                "De contactkoppeling stopte door een onverwachte fout. De plan- en pandgegevens blijven beschikbaar."
            )
        )
    if len(_pand_cache) > 200:
        _pand_cache.clear()
    _pand_cache[cleaned] = resultaat
    return resultaat


def gemeenten(term: str) -> dict:
    cleaned = cql_term(term, 40).strip('"')
    if len(cleaned) < 2:
        return {"gemeenten": []}
    params = urllib.parse.urlencode({"q": cleaned, "fq": "type:gemeente", "rows": "8"})
    try:
        raw = fetch(LOCATIE_URL + "?" + params, accept="application/json")
    except (urllib.error.URLError, urllib.error.HTTPError):
        return {"gemeenten": []}
    data = json.loads(raw.decode("utf-8"))
    names = []
    for doc in data.get("response", {}).get("docs", []):
        name = doc.get("weergavenaam", "")
        short = re.sub(r"^Gemeente\s+", "", name).strip()
        if short and short not in names:
            names.append(short)
    return {"gemeenten": names}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/zoek":
            params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            self.send_json(search(params))
            return
        if parsed.path == "/api/gemeenten":
            params = urllib.parse.parse_qs(parsed.query)
            self.send_json(gemeenten(params.get("q", [""])[0]))
            return
        if parsed.path == "/api/pand":
            params = urllib.parse.parse_qs(parsed.query)
            self.send_json(pand(params.get("adres", [""])[0]))
            return
        if parsed.path == "/api/panden":
            params = {key: value[0] for key, value in urllib.parse.parse_qs(parsed.query).items()}
            self.send_json(bestemmingbron.zoek(params))
            return
        self.send_json({"fout": "Niet gevonden."}, 404)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Scraper staat op http://{HOST}:{PORT}", flush=True)
    print(f"Standaarddatum in de interface: afgelopen jaar vanaf {date.today().isoformat()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nGestopt.")


if __name__ == "__main__":
    main()
