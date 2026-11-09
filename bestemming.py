"""Adressen zoeken op een logiesbestemming in een gekozen regio.

De vlakken komen uit de open bestemmingsplankaart van PDOK (halfjaarlijks
bijgewerkt). De adressen komen uit de BAG. Een nieuw omgevingsplan dat het
bestemmingsplan heeft vervangen staat niet in deze kaart.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

USER_AGENT = "Scraper/0.2 (bestemming en BAG)"
WMS = "https://service.pdok.nl/kadaster/plu/wms/v1_0"
TILES = "https://service.pdok.nl/omgevingswet/ruimtelijkeplannen/api/v1_0/plannen"
LOCATIE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
BAG = "https://api.pdok.nl/kadaster/bag/ogc/v2/collections/adres/items"
GRENZEN = "https://api.pdok.nl/kadaster/bestuurlijkegebieden/ogc/v1/collections/gemeentegebied/items"
PAGE_SIZE = 15
MAX_MATCHES = 400
ORIGIN_X = -285401.92
ORIGIN_Y = 903401.92
TERMS = ("logies", "hotel", "kamerverhuur", "kamerbewoning")
PLAN_TYPES = {"bestemmingsplan", "wijzigingsplan", "uitwerkingsplan", "beheersverordening", "inpassingsplan"}
PLAN_ID = re.compile(r"NL\.IMRO\.\d{4}\.[A-Za-z0-9]+-[A-Za-z0-9]+")
_cache: dict[str, tuple[float, dict]] = {}


def fetch(url: str, accept: str = "*/*", timeout: int = 25) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def locatie(query: str, soort: str) -> dict | None:
    url = LOCATIE + "?" + urllib.parse.urlencode(
        {"q": query, "fq": f"type:{soort}", "rows": "1", "fl": "weergavenaam,centroide_ll,centroide_rd,gemeentenaam,woonplaatsnaam"}
    )
    status, raw = fetch(url, "application/json")
    if status != 200:
        return None
    docs = json.loads(raw.decode("utf-8")).get("response", {}).get("docs") or []
    return docs[0] if docs else None


def wgs_naar_rd(lat: float, lon: float) -> tuple[float, float]:
    phi = 0.36 * (lat - 52.15517440)
    lam = 0.36 * (lon - 5.38720621)
    x = 155000.0
    y = 463000.0
    for power_phi, power_lam, coefficient in (
        (0, 1, 190094.945), (1, 1, -11832.228), (2, 1, -114.221), (0, 3, -32.391),
        (1, 0, -0.705), (3, 1, -2.340), (1, 3, -0.008), (2, 3, 0.0), (0, 2, -0.608), (2, 0, 0.148),
    ):
        x += coefficient * phi ** power_phi * lam ** power_lam
    for power_phi, power_lam, coefficient in (
        (1, 0, 309056.544), (0, 2, 3638.893), (2, 0, 73.077), (1, 2, -157.984),
        (3, 0, 59.788), (0, 1, 0.433), (2, 2, -6.439), (1, 1, -0.032), (0, 4, 0.092), (3, 2, -0.054),
    ):
        y += coefficient * phi ** power_phi * lam ** power_lam
    return x, y


def rd_naar_wgs(x: float, y: float) -> tuple[float, float]:
    dx = (x - 155000) * 1e-5
    dy = (y - 463000) * 1e-5
    lat = 52.15517440
    lon = 5.38720621
    for power_x, power_y, coefficient in (
        (0, 1, 3235.65389), (2, 0, -32.58297), (0, 2, -0.24750), (2, 1, -0.84978),
        (0, 3, -0.06550), (2, 2, -0.01709), (1, 0, -0.00738), (4, 0, 0.00530),
        (2, 3, -0.00039), (4, 1, 0.00033), (1, 1, -0.00012),
    ):
        lat += (coefficient * dx ** power_x * dy ** power_y) / 3600
    for power_x, power_y, coefficient in (
        (1, 0, 5260.52916), (1, 1, 105.94684), (1, 2, 2.45656), (3, 0, -0.81885),
        (1, 3, 0.05594), (3, 1, -0.05607), (0, 1, 0.01199), (3, 2, -0.00256),
        (1, 4, 0.00128), (0, 2, 0.00022), (2, 0, -0.00022), (5, 0, 0.00026),
    ):
        lon += (coefficient * dx ** power_x * dy ** power_y) / 3600
    return lat, lon


def punt(value: str) -> tuple[float, float] | None:
    match = re.search(r"POINT\(([0-9.]+)\s+([0-9.]+)\)", value or "")
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def gebied(params: dict) -> tuple[dict | None, str | None]:
    plaats = re.sub(r"\s+", " ", params.get("plaats") or "").strip()[:60]
    gemeente = re.sub(r"\s+", " ", params.get("gemeente") or "").strip()[:60]
    try:
        straal = float(params.get("straal") or 0)
    except ValueError:
        straal = 0
    if plaats:
        doc = locatie(plaats, "woonplaats") or locatie(plaats, "gemeente")
        if not doc:
            return None, "Die plaats is niet gevonden."
        lon, lat = punt(doc.get("centroide_ll", "")) or (None, None)
        if lat is None:
            return None, "Die plaats heeft geen kaartpunt."
        kilometers = straal if straal > 0 else 2
        if kilometers > 15:
            kilometers = 15
        x, y = wgs_naar_rd(lat, lon)
        meters = kilometers * 1000
        return {
            "minx": x - meters, "miny": y - meters, "maxx": x + meters, "maxy": y + meters,
            "cx": x, "cy": y, "straal": meters,
            "label": f"{doc.get('woonplaatsnaam') or doc.get('weergavenaam')} · {kilometers:g} km",
        }, None
    if gemeente:
        vak, fout = gemeente_vak(gemeente)
        if fout:
            return None, fout
        return vak, None
    return None, "Kies een plaats of een gemeente."


def gemeente_vak(naam: str) -> tuple[dict | None, str | None]:
    url = GRENZEN + "?" + urllib.parse.urlencode({
        "f": "json",
        "limit": "5",
        "skipGeometry": "true",
        "filter-lang": "cql-text",
        "filter": f"naam ILIKE '{naam.replace(chr(39), '')}'",
    })
    status, raw = fetch(url, "application/json", 30)
    if status != 200:
        return None, "De gemeentegrens kon niet worden opgehaald."
    features = json.loads(raw.decode("utf-8")).get("features") or []
    gekozen = None
    for feature in features:
        props = feature.get("properties") or {}
        if (props.get("naam") or "").casefold() == naam.casefold():
            gekozen = feature
            break
    if gekozen is None and features:
        gekozen = features[0]
    if not gekozen or not gekozen.get("bbox"):
        doc = locatie(naam, "gemeente")
        if not doc:
            return None, "Die gemeente is niet gevonden."
        lon, lat = punt(doc.get("centroide_ll", "")) or (None, None)
        if lat is None:
            return None, "Die gemeente heeft geen kaartpunt."
        x, y = wgs_naar_rd(lat, lon)
        return {"minx": x - 6000, "miny": y - 6000, "maxx": x + 6000, "maxy": y + 6000, "cx": None, "cy": None, "straal": None, "label": naam}, None
    min_lon, min_lat, max_lon, max_lat = gekozen["bbox"]
    minx, miny = wgs_naar_rd(min_lat, min_lon)
    maxx, maxy = wgs_naar_rd(max_lat, max_lon)
    return {
        "minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy,
        "cx": None, "cy": None, "straal": None,
        "label": (gekozen.get("properties") or {}).get("naam") or naam,
    }, None


def termen(params: dict) -> list[str]:
    ruw = params.get("bestemming") or ",".join(TERMS)
    gekozen = []
    for deel in re.split(r"[,+]", ruw):
        woord = deel.strip().casefold()
        if woord in TERMS and woord not in gekozen:
            gekozen.append(woord)
    return gekozen or list(TERMS)


def past_bij(naam: str, gekozen: list[str]) -> bool:
    tekst = (naam or "").casefold()
    if not tekst:
        return False
    if any(woord in tekst for woord in ("verbod", "uitgesloten", "niet toegestaan", "uit te sluiten")):
        return False
    return any(woord in tekst for woord in gekozen)


def plannen(vak: dict) -> list[str]:
    breedte = max(vak["maxx"] - vak["minx"], vak["maxy"] - vak["miny"])
    stap = min(1200, max(400, breedte / 4))
    punten = []
    x = vak["minx"]
    while x <= vak["maxx"] and len(punten) < 24:
        y = vak["miny"]
        while y <= vak["maxy"] and len(punten) < 24:
            punten.append((x, y))
            y += stap
        x += stap
    if vak.get("cx") is not None:
        punten.append((vak["cx"], vak["cy"]))
    gevonden: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        taken = [pool.submit(plan_info, x, y) for x, y in punten]
        for taak in as_completed(taken):
            for plan_id, soort in taak.result():
                if soort in PLAN_TYPES:
                    gevonden[plan_id] = soort
    return list(gevonden)


def plan_info(x: float, y: float) -> list[tuple[str, str]]:
    url = (
        f"{WMS}?service=WMS&version=1.3.0&request=GetFeatureInfo"
        "&layers=bestemmingsplangebied&query_layers=bestemmingsplangebied"
        "&info_format=text/plain&crs=EPSG:28992"
        f"&bbox={x - 15},{y - 15},{x + 15},{y + 15}&width=31&height=31&i=15&j=15&feature_count=12"
    )
    status, raw = fetch(url, "text/plain", 20)
    if status != 200:
        return []
    tekst = raw.decode("utf-8", "replace")
    resultaten = []
    for blok in tekst.split("Feature ")[1:]:
        identificatie = re.search(r"identificatie = '([^']+)'", blok)
        soort = re.search(r"typeplan = '([^']+)'", blok)
        historisch = re.search(r"historisch = '([^']*)'", blok)
        status_plan = re.search(r"planstatus = '([^']*)'", blok)
        if not identificatie or not PLAN_ID.fullmatch(identificatie.group(1)):
            continue
        if historisch and historisch.group(1) not in ("0", "nee", ""):
            continue
        if status_plan and status_plan.group(1).casefold() in ("ontwerp", "concept", "voorontwerp"):
            continue
        resultaten.append((identificatie.group(1), (soort.group(1) if soort else "").casefold()))
    return resultaten


def zoom_voor(vak: dict) -> int:
    breedte = max(vak["maxx"] - vak["minx"], vak["maxy"] - vak["miny"])
    for niveau in (11, 10, 9, 8):
        span = (3440.64 / (2 ** niveau)) * 256
        aantal = math.ceil(breedte / span + 1) ** 2
        if aantal <= 20 or niveau == 8:
            return niveau
    return 8


def tegel_bereik(vak: dict, zoom: int) -> list[tuple[int, int]]:
    span = (3440.64 / (2 ** zoom)) * 256
    x0 = math.floor((vak["minx"] - ORIGIN_X) / span)
    x1 = math.floor((vak["maxx"] - ORIGIN_X) / span)
    y0 = math.floor((ORIGIN_Y - vak["maxy"]) / span)
    y1 = math.floor((ORIGIN_Y - vak["miny"]) / span)
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def lees_varint(buf: bytes, index: int) -> tuple[int, int]:
    shift = 0
    number = 0
    while True:
        byte = buf[index]
        index += 1
        number |= (byte & 0x7F) << shift
        if byte < 128:
            return number, index
        shift += 7


def velden(buf: bytes) -> list[tuple[int, int, object]]:
    index = 0
    found = []
    while index < len(buf):
        key, index = lees_varint(buf, index)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, index = lees_varint(buf, index)
            found.append((field, wire, value))
        elif wire == 2:
            length, index = lees_varint(buf, index)
            found.append((field, wire, buf[index:index + length]))
            index += length
        elif wire == 5:
            found.append((field, wire, buf[index:index + 4]))
            index += 4
        elif wire == 1:
            found.append((field, wire, buf[index:index + 8]))
            index += 8
        else:
            break
    return found


def zigzag(number: int) -> int:
    return (number >> 1) ^ -(number & 1)


def waarde(raw: bytes):
    for field, wire, value in velden(raw):
        if field == 1 and wire == 2:
            return value.decode()
        if field == 7:
            return bool(value)
        if field in (4, 5, 6) and wire == 0:
            number = zigzag(value) if field == 6 else value
            if number >= 2 ** 63:
                number -= 2 ** 64
            return number
        if field in (2, 3):
            return None
    return None


def packed(raw: bytes) -> list[int]:
    index = 0
    numbers = []
    while index < len(raw):
        number, index = lees_varint(raw, index)
        numbers.append(number)
    return numbers


def ringen(nums: list[int]) -> list[list[tuple[int, int]]]:
    index = 0
    cursor_x = cursor_y = 0
    rings: list[list[tuple[int, int]]] = []
    ring: list[tuple[int, int]] = []
    while index < len(nums):
        command = nums[index]
        index += 1
        kind = command & 7
        count = command >> 3
        if kind in (1, 2):
            if kind == 1 and ring:
                rings.append(ring)
                ring = []
            for _ in range(count):
                if index + 1 >= len(nums):
                    return rings
                cursor_x += zigzag(nums[index])
                cursor_y += zigzag(nums[index + 1])
                index += 2
                ring.append((cursor_x, cursor_y))
        elif kind == 7:
            if ring:
                rings.append(ring)
                ring = []
        else:
            break
    if ring:
        rings.append(ring)
    return rings


def naar_rd(zoom: int, tile_x: int, tile_y: int, rings: list[list[tuple[int, int]]], extent: int) -> list[list[tuple[float, float]]]:
    span = (3440.64 / (2 ** zoom)) * 256
    west = ORIGIN_X + tile_x * span
    north = ORIGIN_Y - tile_y * span
    omgezet = []
    for ring in rings:
        omgezet.append([
            (west + px / extent * span, north - py / extent * span)
            for px, py in ring
        ])
    return omgezet


def vlakken_uit_tegel(blob: bytes, zoom: int, tile_x: int, tile_y: int, gekozen: list[str]) -> list[dict]:
    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
    vlakken = []
    for field, wire, layer in velden(blob):
        if field != 3 or wire != 2:
            continue
        parts = velden(layer)
        if next((value.decode() for number, kind, value in parts if number == 1 and kind == 2), "") != "planobject_polygon":
            continue
        keys = [value.decode() for number, kind, value in parts if number == 3 and kind == 2]
        values = [waarde(value) for number, kind, value in parts if number == 4 and kind == 2]
        extent = next((value for number, kind, value in parts if number == 5 and kind == 0), 4096)
        for feature in (value for number, kind, value in parts if number == 2 and kind == 2):
            tags: list[int] = []
            geometry: list[int] = []
            for number, kind, value in velden(feature):
                if number == 2 and kind == 2:
                    tags = packed(value)
                elif number == 4 and kind == 2:
                    geometry = packed(value)
                elif number == 2 and kind == 0:
                    tags.append(value)
            props = {}
            for offset in range(0, len(tags) - 1, 2):
                key_index, value_index = tags[offset], tags[offset + 1]
                if key_index < len(keys) and value_index < len(values):
                    props[keys[key_index]] = values[value_index]
            naam = str(props.get("naam") or props.get("classificatie") or "")
            if not past_bij(naam, gekozen):
                continue
            rings = naar_rd(zoom, tile_x, tile_y, ringen(geometry), int(extent or 4096))
            if rings:
                vlakken.append({
                    "naam": naam,
                    "categorie": str(props.get("categorie") or props.get("type") or ""),
                    "plan": str(props.get("planid") or ""),
                    "ringen": rings,
                })
    return vlakken


def tegel(plan_id: str, zoom: int, tile_x: int, tile_y: int, gekozen: list[str]) -> list[dict]:
    url = f"{TILES}/{urllib.parse.quote(plan_id)}/tiles/{zoom}/{tile_x}/{tile_y}.mvt"
    status, raw = fetch(url, "application/vnd.mapbox-vector-tile", 20)
    if status != 200 or not raw:
        return []
    try:
        return vlakken_uit_tegel(raw, zoom, tile_x, tile_y, gekozen)
    except Exception:
        return []


def plan_vak(plan_id: str) -> dict | None:
    status, raw = fetch(f"{TILES}/{urllib.parse.quote(plan_id)}/", "application/json", 20)
    if status != 200:
        return None
    try:
        bounds = [float(deel) for deel in json.loads(raw.decode("utf-8")).get("bounds", "").split(",")]
    except ValueError:
        return None
    if len(bounds) != 4:
        return None
    xs, ys = [], []
    for lon in (bounds[0], bounds[2]):
        for lat in (bounds[1], bounds[3]):
            x, y = wgs_naar_rd(lat, lon)
            xs.append(x)
            ys.append(y)
    return {"minx": min(xs), "miny": min(ys), "maxx": max(xs), "maxy": max(ys)}


def doorsnede(links: dict, rechts: dict) -> dict | None:
    vak = {
        "minx": max(links["minx"], rechts["minx"]),
        "miny": max(links["miny"], rechts["miny"]),
        "maxx": min(links["maxx"], rechts["maxx"]),
        "maxy": min(links["maxy"], rechts["maxy"]),
    }
    if vak["minx"] >= vak["maxx"] or vak["miny"] >= vak["maxy"]:
        return None
    return vak


def vlakken(plan_ids: list[str], vak: dict, gekozen: list[str]) -> list[dict]:
    taken = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        grenzen = list(pool.map(plan_vak, plan_ids))
    for plan_id, grens in zip(plan_ids, grenzen):
        stuk = doorsnede(vak, grens) if grens else None
        if not stuk:
            continue
        zoom = 11 if max(stuk["maxx"] - stuk["minx"], stuk["maxy"] - stuk["miny"]) <= 1800 else zoom_voor(stuk)
        for x, y in tegel_bereik(stuk, zoom)[:24]:
            taken.append((plan_id, zoom, x, y))
    gevonden = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        toekomst = [pool.submit(tegel, plan_id, zoom, x, y, gekozen) for plan_id, zoom, x, y in taken]
        for taak in as_completed(toekomst):
            gevonden.extend(taak.result())
    uniek = {}
    for vlak in gevonden:
        sleutel = (vlak["plan"], vlak["naam"], round(vlak["ringen"][0][0][0]), round(vlak["ringen"][0][0][1]))
        uniek[sleutel] = vlak
    return list(uniek.values())


def binnen(x: float, y: float, rings: list[list[tuple[float, float]]]) -> bool:
    geraakt = False
    for ring in rings:
        vorige = len(ring) - 1
        for index, (xi, yi) in enumerate(ring):
            xj, yj = ring[vorige]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
                geraakt = not geraakt
            vorige = index
    return geraakt


def overlapt(vlak: dict, vak: dict) -> bool:
    xs = [x for ring in vlak["ringen"] for x, _ in ring]
    ys = [y for ring in vlak["ringen"] for _, y in ring]
    if not xs:
        return False
    return not (max(xs) < vak["minx"] or min(xs) > vak["maxx"] or max(ys) < vak["miny"] or min(ys) > vak["maxy"])


def adres_tekst(props: dict) -> str:
    nummer = str(props.get("huisnummer") or "")
    letter = props.get("huisletter") or ""
    toevoeging = props.get("toevoeging") or ""
    extra = f"{letter}{('-' + toevoeging) if toevoeging else ''}"
    straat = props.get("openbare_ruimte_naam") or ""
    postcode = (props.get("postcode") or "").replace(" ", "")
    plaats = props.get("woonplaats_naam") or ""
    return re.sub(r"\s+", " ", f"{straat} {nummer}{extra}, {postcode} {plaats}").strip(" ,")


def afstand(x: float, y: float, rings: list[list[tuple[float, float]]]) -> float:
    best = 1e18
    for ring in rings:
        for index, (x1, y1) in enumerate(ring):
            x2, y2 = ring[(index + 1) % len(ring)]
            dx, dy = x2 - x1, y2 - y1
            lengte = dx * dx + dy * dy or 1.0
            t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / lengte))
            px, py = x1 + t * dx, y1 + t * dy
            best = min(best, (x - px) ** 2 + (y - py) ** 2)
    return math.sqrt(best)


def bag_in_vak(minx: float, miny: float, maxx: float, maxy: float, paginas: int) -> list[dict]:
    zuid, west = rd_naar_wgs(minx, miny)
    noord, oost = rd_naar_wgs(maxx, maxy)
    url = BAG + "?" + urllib.parse.urlencode(
        {"f": "json", "limit": "200", "bbox": f"{min(west, oost)},{min(zuid, noord)},{max(west, oost)},{max(zuid, noord)}"},
        safe=",",
    )
    features = []
    for _ in range(paginas):
        status, raw = fetch(url, "application/json", 30)
        if status != 200:
            break
        data = json.loads(raw.decode("utf-8"))
        batch = data.get("features") or []
        features.extend(batch)
        volgende = next((link.get("href") for link in data.get("links") or [] if link.get("rel") == "next"), "")
        if len(batch) < 200 or not volgende:
            break
        url = volgende
    return features


def adressen_van_vlak(vlak: dict, vak: dict) -> list[dict]:
    xs = [x for ring in vlak["ringen"] for x, _ in ring]
    ys = [y for ring in vlak["ringen"] for _, y in ring]
    if not xs:
        return []
    groot = max(xs) - min(xs) > 700 or max(ys) - min(ys) > 700
    marge = 0 if groot else 20
    minx = max(min(xs) - marge, vak["minx"] - marge)
    maxx = min(max(xs) + marge, vak["maxx"] + marge)
    miny = max(min(ys) - marge, vak["miny"] - marge)
    maxy = min(max(ys) + marge, vak["maxy"] + marge)
    if minx >= maxx or miny >= maxy:
        return []
    gevonden = []
    for feature in bag_in_vak(minx, miny, maxx, maxy, 6 if groot else 2):
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        x, y = wgs_naar_rd(lat, lon)
        if vak.get("straal") and vak.get("cx") is not None:
            if (x - vak["cx"]) ** 2 + (y - vak["cy"]) ** 2 > vak["straal"] ** 2:
                continue
        if not binnen(x, y, vlak["ringen"]) and (groot or afstand(x, y, vlak["ringen"]) > 12):
            continue
        props = feature.get("properties") or {}
        adres = adres_tekst(props)
        if not adres:
            continue
        gevonden.append({
            "adres": adres,
            "bestemming": vlak["naam"],
            "soort": vlak["categorie"],
            "plan": vlak["plan"],
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "gemeente": props.get("bronhouder_naam") or "",
            "woonplaats": props.get("woonplaats_naam") or "",
        })
    return gevonden


def adressen(vlak_lijst: list[dict], vak: dict, gekozen: list[str]) -> tuple[list[dict], bool]:
    resultaten = {}
    afgekapt = False
    with ThreadPoolExecutor(max_workers=8) as pool:
        taken = [pool.submit(adressen_van_vlak, vlak, vak) for vlak in vlak_lijst[:80]]
        for taak in as_completed(taken):
            for item in taak.result():
                bestaand = resultaten.get(item["adres"])
                if bestaand:
                    if item["bestemming"] not in bestaand["bestemming"]:
                        bestaand["bestemming"] += ", " + item["bestemming"]
                    continue
                if len(resultaten) >= MAX_MATCHES:
                    afgekapt = True
                    continue
                resultaten[item["adres"]] = item
    return list(resultaten.values()), afgekapt


def zoek(params: dict) -> dict:
    gekozen = termen(params)
    try:
        page = max(1, int(params.get("page", "1")))
    except ValueError:
        page = 1
    vak, fout = gebied(params)
    if fout:
        return {"fout": fout}
    sleutel = json.dumps({"vak": {k: round(v, 1) if isinstance(v, float) else v for k, v in vak.items()}, "termen": gekozen}, sort_keys=True)
    nu = time.time()
    gecached = _cache.get(sleutel)
    if gecached and nu - gecached[0] < 600:
        bundel = gecached[1]
    else:
        try:
            plan_ids = plannen(vak)
            vlak_lijst = [vlak for vlak in vlakken(plan_ids, vak, gekozen) if overlapt(vlak, vak)]
            matches, afgekapt = adressen(vlak_lijst, vak, gekozen) if vlak_lijst else ([], False)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            return {"fout": "De bestemmingskaart of de BAG antwoordt niet."}
        matches.sort(key=lambda item: item["adres"])
        bundel = {"matches": matches, "afgekapt": afgekapt, "plannen": len(plan_ids), "vlakken": len(vlak_lijst)}
        if len(_cache) > 12:
            _cache.clear()
        _cache[sleutel] = (nu, bundel)
    matches = bundel["matches"]
    start = (page - 1) * PAGE_SIZE
    return {
        "totaal": len(matches),
        "pagina": page,
        "perPagina": PAGE_SIZE,
        "afgekapt": bundel["afgekapt"],
        "gebied": vak["label"],
        "bestemming": gekozen,
        "bron": "Vastgesteld bestemmingsplan via PDOK, halfjaarlijks bijgewerkt. Adressen uit de BAG.",
        "resultaten": matches[start:start + PAGE_SIZE],
    }
