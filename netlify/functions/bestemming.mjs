import { gunzipSync } from "node:zlib";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const USER_AGENT = "Scraper/0.2 (bestemming en BAG)";
const WMS = "https://service.pdok.nl/kadaster/plu/wms/v1_0";
const TILES = "https://service.pdok.nl/omgevingswet/ruimtelijkeplannen/api/v1_0/plannen";
const LOCATIE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free";
const BAG = "https://api.pdok.nl/kadaster/bag/ogc/v2/collections/adres/items";
const GRENZEN = "https://api.pdok.nl/kadaster/bestuurlijkegebieden/ogc/v1/collections/gemeentegebied/items";
const PROVINCIES_GRENZEN = "https://api.pdok.nl/kadaster/bestuurlijkegebieden/ogc/v1/collections/provinciegebied/items";
const PROVINCIES = new Set(["Drenthe", "Flevoland", "Fryslân", "Gelderland", "Groningen", "Limburg", "Noord-Brabant", "Noord-Holland", "Overijssel", "Utrecht", "Zeeland", "Zuid-Holland"]);
const VOORGANGER = {
  "0014": ["0040"],
  "0361": ["0365"],
  "0484": ["0499"],
  "0575": ["0576"],
  "1901": ["0497"],
  "1940": ["0055"],
  "1945": ["0282"],
  "1949": ["0063", "0070", "1908"],
  "1950": ["0048"],
  "1952": ["0040", "1987"],
  "1959": ["0000"],
  "1960": ["0236", "0733"],
  "1961": ["0545", "0707"],
  "1963": ["0584", "0585"],
  "1966": ["1651"],
  "1970": ["0058", "1722"],
  "1979": ["0003", "0010"],
  "1980": ["0416"],
  "1982": ["0756", "0815", "1702"],
  "1992": ["0501", "0614"],
};
const provincieCache = new Map();
const PAGE_SIZE = 15;
const MAX_MATCHES = 400;
const ORIGIN_X = -285401.92;
const ORIGIN_Y = 903401.92;
const TERMS = ["logies", "hotel", "kamerverhuur", "kamerbewoning", "horeca"];
const PLAN_TYPES = new Set(["bestemmingsplan", "wijzigingsplan", "uitwerkingsplan", "beheersverordening", "inpassingsplan"]);
const PLAN_ID = /^NL\.IMRO\.\d{4}\.[A-Za-z0-9]+-[A-Za-z0-9]+$/;
const cache = new Map();
const INDEX = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "plannen-index.json"), "utf8"));

function plannenUitIndex(code, gekozen) {
  const codes = [code, ...(VOORGANGER[code] || [])];
  const ids = [];
  for (const gemeente of codes) {
    const termen = INDEX[gemeente] || {};
    for (const term of gekozen) {
      for (const planId of termen[term] || []) {
        if (!ids.includes(planId)) ids.push(planId);
      }
    }
  }
  return ids;
}

async function fetchBytes(url, timeout = 25000) {
  const response = await fetch(url, {
    headers: { "User-Agent": USER_AGENT, Accept: "*/*" },
    signal: AbortSignal.timeout(timeout),
  });
  return [response.status, new Uint8Array(await response.arrayBuffer())];
}

async function fetchJson(url, timeout = 25000) {
  const [status, raw] = await fetchBytes(url, timeout);
  if (status !== 200) return [status, null];
  return [status, JSON.parse(new TextDecoder().decode(raw))];
}

async function pool(items, limit, worker) {
  const results = new Array(items.length);
  let cursor = 0;
  async function run() {
    while (cursor < items.length) {
      const index = cursor;
      cursor += 1;
      results[index] = await worker(items[index]);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, run));
  return results;
}

function wgsNaarRd(lat, lon) {
  const phi = 0.36 * (lat - 52.15517440);
  const lam = 0.36 * (lon - 5.38720621);
  let x = 155000;
  let y = 463000;
  for (const [p, q, c] of [[0, 1, 190094.945], [1, 1, -11832.228], [2, 1, -114.221], [0, 3, -32.391], [1, 0, -0.705], [3, 1, -2.340], [1, 3, -0.008], [0, 2, -0.608], [2, 0, 0.148]]) {
    x += c * phi ** p * lam ** q;
  }
  for (const [p, q, c] of [[1, 0, 309056.544], [0, 2, 3638.893], [2, 0, 73.077], [1, 2, -157.984], [3, 0, 59.788], [0, 1, 0.433], [2, 2, -6.439], [1, 1, -0.032], [0, 4, 0.092], [3, 2, -0.054]]) {
    y += c * phi ** p * lam ** q;
  }
  return [x, y];
}

function rdNaarWgs(x, y) {
  const dx = (x - 155000) * 1e-5;
  const dy = (y - 463000) * 1e-5;
  let lat = 52.15517440;
  let lon = 5.38720621;
  for (const [p, q, c] of [[0, 1, 3235.65389], [2, 0, -32.58297], [0, 2, -0.2475], [2, 1, -0.84978], [0, 3, -0.0655], [2, 2, -0.01709], [1, 0, -0.00738], [4, 0, 0.0053], [2, 3, -0.00039], [4, 1, 0.00033], [1, 1, -0.00012]]) {
    lat += (c * dx ** p * dy ** q) / 3600;
  }
  for (const [p, q, c] of [[1, 0, 5260.52916], [1, 1, 105.94684], [1, 2, 2.45656], [3, 0, -0.81885], [1, 3, 0.05594], [3, 1, -0.05607], [0, 1, 0.01199], [3, 2, -0.00256], [1, 4, 0.00128], [0, 2, 0.00022], [2, 0, -0.00022], [5, 0, 0.00026]]) {
    lon += (c * dx ** p * dy ** q) / 3600;
  }
  return [lat, lon];
}

function punt(value) {
  const match = /POINT\(([0-9.]+)\s+([0-9.]+)\)/.exec(value || "");
  return match ? [Number(match[1]), Number(match[2])] : null;
}

async function locatie(query, soort) {
  const url = `${LOCATIE}?${new URLSearchParams({ q: query, fq: `type:${soort}`, rows: "1", fl: "weergavenaam,centroide_ll,centroide_rd,gemeentenaam,gemeentecode,woonplaatsnaam" })}`;
  const [status, data] = await fetchJson(url);
  if (status !== 200) return null;
  return data?.response?.docs?.[0] || null;
}

async function gebied(params) {
  const plaats = String(params.plaats || "").replace(/\s+/g, " ").trim().slice(0, 60);
  const gemeente = String(params.gemeente || "").replace(/\s+/g, " ").trim().slice(0, 60);
  let straal = Number(params.straal || 0);
  if (!Number.isFinite(straal)) straal = 0;
  if (plaats) {
    const doc = await locatie(plaats, "woonplaats") || await locatie(plaats, "gemeente");
    if (!doc) return [null, "Die plaats is niet gevonden."];
    const pair = punt(doc.centroide_ll);
    if (!pair) return [null, "Die plaats heeft geen kaartpunt."];
    const kilometers = straal > 0 ? Math.min(straal, 15) : 2;
    const [x, y] = wgsNaarRd(pair[1], pair[0]);
    const meters = kilometers * 1000;
    return [{
      minx: x - meters, miny: y - meters, maxx: x + meters, maxy: y + meters,
      cx: x, cy: y, straal: meters,
      label: `${doc.woonplaatsnaam || doc.weergavenaam} · ${kilometers} km`,
    }, null];
  }
  if (gemeente) return gemeenteVak(gemeente);
  const provincie = String(params.provincie || "").trim();
  if (provincie) return provincieVak(provincie);
  return [null, "Kies een plaats, een gemeente of een provincie."];
}

function uitersten(coordinates) {
  const xs = [];
  const ys = [];
  const loop = (node) => {
    if (Array.isArray(node) && node.length && typeof node[0] === "number") {
      xs.push(node[0]);
      ys.push(node[1]);
    } else if (Array.isArray(node)) node.forEach(loop);
  };
  loop(coordinates);
  return [xs, ys];
}

async function gemeenteVak(naam) {
  const doc = await locatie(naam, "gemeente");
  if (!doc) return [null, "Die gemeente is niet gevonden."];
  const officieel = String(doc.weergavenaam || naam).replace(/^Gemeente\s+/i, "").trim();
  const url = `${GRENZEN}?${new URLSearchParams({
    f: "json", limit: "1", "filter-lang": "cql2-text",
    filter: `naam = '${officieel.replace(/'/g, "")}'`,
  })}`;
  const [status, data] = await fetchJson(url, 30000);
  const feature = status === 200 ? data?.features?.[0] : null;
  const [xs, ys] = uitersten(feature?.geometry?.coordinates);
  if (xs.length) {
    const hoeken = [wgsNaarRd(Math.min(...ys), Math.min(...xs)), wgsNaarRd(Math.min(...ys), Math.max(...xs)), wgsNaarRd(Math.max(...ys), Math.min(...xs)), wgsNaarRd(Math.max(...ys), Math.max(...xs))];
    return [{
      minx: Math.min(...hoeken.map(([x]) => x)),
      miny: Math.min(...hoeken.map(([, y]) => y)),
      maxx: Math.max(...hoeken.map(([x]) => x)),
      maxy: Math.max(...hoeken.map(([, y]) => y)),
      cx: null, cy: null, straal: null, label: officieel, gemeente: officieel, code: doc.gemeentecode || "",
    }, null];
  }
  const pair = punt(doc.centroide_ll);
  if (!pair) return [null, "Die gemeente heeft geen kaartpunt."];
  const [x, y] = wgsNaarRd(pair[1], pair[0]);
  return [{ minx: x - 8000, miny: y - 8000, maxx: x + 8000, maxy: y + 8000, cx: null, cy: null, straal: null, label: officieel, gemeente: officieel, code: doc.gemeentecode || "" }, null];
}

async function gemeentenVanProvincie(naam) {
  if (provincieCache.has(naam)) return provincieCache.get(naam);
  const url = `${LOCATIE}?${new URLSearchParams([
    ["q", "*"],
    ["fq", "type:gemeente"],
    ["fq", `provincienaam:${naam}`],
    ["rows", "100"],
    ["fl", "gemeentecode,gemeentenaam,centroide_rd"],
    ["sort", "gemeentenaam asc"],
  ])}`;
  const [status, data] = await fetchJson(url, 30000);
  const gemeenten = status === 200 ? (data?.response?.docs || []).flatMap((doc) => {
    const code = String(doc.gemeentecode || "");
    const gemeentenaam = String(doc.gemeentenaam || "").trim();
    return code && gemeentenaam ? [{ code, naam: gemeentenaam, rd: doc.centroide_rd || "" }] : [];
  }) : [];
  provincieCache.set(naam, gemeenten);
  return gemeenten;
}

function rdOmvang(coordinates) {
  let minx = Infinity;
  let miny = Infinity;
  let maxx = -Infinity;
  let maxy = -Infinity;
  let aantal = 0;
  const loop = (node) => {
    if (Array.isArray(node) && node.length >= 2 && typeof node[0] === "number") {
      const [x, y] = wgsNaarRd(node[1], node[0]);
      if (x < minx) minx = x;
      if (y < miny) miny = y;
      if (x > maxx) maxx = x;
      if (y > maxy) maxy = y;
      aantal += 1;
    } else if (Array.isArray(node)) node.forEach(loop);
  };
  loop(coordinates);
  if (!aantal) return null;
  return [minx, miny, maxx, maxy];
}

async function provincieVak(naam) {
  if (!PROVINCIES.has(naam)) return [null, "Kies een provincie uit de lijst."];
  const gemeenten = await gemeentenVanProvincie(naam);
  if (!gemeenten.length) return [null, "Voor die provincie zijn geen gemeenten gevonden."];
  const url = `${PROVINCIES_GRENZEN}?${new URLSearchParams({
    f: "json", limit: "1", "filter-lang": "cql2-text",
    filter: `naam = '${naam.replace(/'/g, "")}'`,
  })}`;
  const [status, data] = await fetchJson(url, 30000);
  let omvang = status === 200 ? rdOmvang(data?.features?.[0]?.geometry?.coordinates) : null;
  if (!omvang) {
    const punten = gemeenten.flatMap((gemeente) => {
      const rd = punt(gemeente.rd);
      return rd ? [rd] : [];
    });
    if (!punten.length) return [null, "Die provincie heeft geen kaartvlak."];
    omvang = [
      Math.min(...punten.map(([x]) => x)) - 12000,
      Math.min(...punten.map(([, y]) => y)) - 12000,
      Math.max(...punten.map(([x]) => x)) + 12000,
      Math.max(...punten.map(([, y]) => y)) + 12000,
    ];
  }
  const [minx, miny, maxx, maxy] = omvang;
  return [{
    minx: minx - 2000, miny: miny - 2000, maxx: maxx + 2000, maxy: maxy + 2000,
    cx: null, cy: null, straal: null, label: naam, gemeente: "", code: "", provincie: naam,
    codes: gemeenten.map((gemeente) => gemeente.code),
    gemeenten: gemeenten.map((gemeente) => gemeente.naam),
  }, null];
}

function plannenVanProvincie(codes, gekozen, limiet) {
  const emmers = codes.map((code) => plannenUitIndex(code, gekozen)).filter((emmer) => emmer.length);
  const gekozenIds = [];
  const gezien = new Set();
  while (gekozenIds.length < limiet && emmers.some((emmer) => emmer.length)) {
    for (const emmer of emmers) {
      if (!emmer.length || gekozenIds.length >= limiet) continue;
      const planId = emmer.shift();
      if (!gezien.has(planId)) {
        gezien.add(planId);
        gekozenIds.push(planId);
      }
    }
  }
  return [gekozenIds, emmers.some((emmer) => emmer.length)];
}

function codesMetVoorganger(codes) {
  const toegestaan = new Set(codes);
  for (const code of codes) for (const oud of VOORGANGER[code] || []) toegestaan.add(oud);
  return toegestaan;
}

function planHoort(planId, codes) {
  const planCode = String(planId).split(".")[2] || "";
  return codes.has(planCode) || planCode.startsWith("99");
}

function geenVlak(vak, gekozen) {
  if (!gekozen.every((term) => term === "kamerverhuur" || term === "kamerbewoning")) return false;
  if (vak.provincie) return !vak.codes.some((code) => plannenUitIndex(code, gekozen).length);
  return Boolean(vak.code) && plannenUitIndex(vak.code || "", gekozen).length === 0;
}

function termen(params) {
  const gekozen = [];
  for (const deel of String(params.bestemming || TERMS.join(",")).split(/[,+]/)) {
    const woord = deel.trim().toLowerCase();
    if (TERMS.includes(woord) && !gekozen.includes(woord)) gekozen.push(woord);
  }
  return gekozen.length ? gekozen : [...TERMS];
}

function pastBij(naam, gekozen) {
  const tekst = String(naam || "").toLowerCase();
  if (!tekst) return false;
  if (["verbod", "uitgesloten", "niet toegestaan", "uit te sluiten"].some((woord) => tekst.includes(woord))) return false;
  return gekozen.some((woord) => tekst.includes(woord));
}

async function planInfo(x, y) {
  const url = `${WMS}?service=WMS&version=1.3.0&request=GetFeatureInfo&layers=bestemmingsplangebied&query_layers=bestemmingsplangebied&info_format=text/plain&crs=EPSG:28992&bbox=${x - 15},${y - 15},${x + 15},${y + 15}&width=31&height=31&i=15&j=15&feature_count=40`;
  const [status, raw] = await fetchBytes(url, 20000);
  if (status !== 200) return [];
  const tekst = new TextDecoder().decode(raw);
  const resultaten = [];
  for (const blok of tekst.split("Feature ").slice(1)) {
    const identificatie = /identificatie = '([^']+)'/.exec(blok);
    const soort = /typeplan = '([^']+)'/.exec(blok);
    const historisch = /historisch = '([^']*)'/.exec(blok);
    const statusPlan = /planstatus = '([^']*)'/.exec(blok);
    if (!identificatie || !PLAN_ID.test(identificatie[1])) continue;
    if (historisch && !["0", "nee", ""].includes(historisch[1])) continue;
    if (statusPlan && ["ontwerp", "concept", "voorontwerp"].includes(statusPlan[1].toLowerCase())) continue;
    resultaten.push([identificatie[1], (soort?.[1] || "").toLowerCase()]);
  }
  return resultaten;
}

async function plannen(vak, maxZijde = 0) {
  const breedte = Math.max(vak.maxx - vak.minx, 1);
  const hoogte = Math.max(vak.maxy - vak.miny, 1);
  const stap = Math.max(breedte, hoogte) < 16000 ? 900 : 2200;
  let kolommen = Math.min(12, Math.max(3, Math.ceil(breedte / stap)));
  let rijen = Math.min(12, Math.max(3, Math.ceil(hoogte / stap)));
  if (maxZijde) {
    kolommen = Math.min(kolommen, maxZijde);
    rijen = Math.min(rijen, maxZijde);
  }
  const punten = [];
  for (let kolom = 0; kolom < kolommen; kolom += 1) {
    for (let rij = 0; rij < rijen; rij += 1) {
      punten.push([
        vak.minx + (kolom + 0.5) * breedte / kolommen,
        vak.miny + (rij + 0.5) * hoogte / rijen,
      ]);
    }
  }
  if (vak.cx != null) punten.push([vak.cx, vak.cy]);
  const gevonden = new Map();
  const blokken = await pool(punten, 8, ([x, y]) => planInfo(x, y));
  for (const blok of blokken) {
    for (const [planId, soort] of blok) {
      if (PLAN_TYPES.has(soort)) gevonden.set(planId, soort);
    }
  }
  return [...gevonden.keys()];
}

function zoomVoor(vak) {
  const breedte = Math.max(vak.maxx - vak.minx, vak.maxy - vak.miny);
  for (const niveau of [11, 10, 9, 8]) {
    const span = (3440.64 / 2 ** niveau) * 256;
    const aantal = Math.ceil(breedte / span + 1) ** 2;
    if (aantal <= 20 || niveau === 8) return niveau;
  }
  return 8;
}

function tegelBereik(vak, zoom) {
  const span = (3440.64 / 2 ** zoom) * 256;
  const x0 = Math.floor((vak.minx - ORIGIN_X) / span);
  const x1 = Math.floor((vak.maxx - ORIGIN_X) / span);
  const y0 = Math.floor((ORIGIN_Y - vak.maxy) / span);
  const y1 = Math.floor((ORIGIN_Y - vak.miny) / span);
  const tegels = [];
  for (let x = x0; x <= x1; x += 1) {
    for (let y = y0; y <= y1; y += 1) tegels.push([x, y]);
  }
  return tegels;
}

function leesVarint(buf, index) {
  let number = 0;
  let shift = 0;
  while (index < buf.length) {
    const byte = buf[index];
    index += 1;
    number += (byte & 0x7f) * 2 ** shift;
    if (byte < 128) return [number, index];
    shift += 7;
  }
  return [number, index];
}

function velden(buf) {
  let index = 0;
  const found = [];
  while (index < buf.length) {
    const [key, next] = leesVarint(buf, index);
    index = next;
    const field = Math.floor(key / 8);
    const wire = key % 8;
    if (wire === 0) {
      const [value, after] = leesVarint(buf, index);
      index = after;
      found.push([field, wire, value]);
    } else if (wire === 2) {
      const [length, after] = leesVarint(buf, index);
      index = after;
      found.push([field, wire, buf.subarray(index, index + length)]);
      index += length;
    } else if (wire === 5) {
      found.push([field, wire, buf.subarray(index, index + 4)]);
      index += 4;
    } else if (wire === 1) {
      found.push([field, wire, buf.subarray(index, index + 8)]);
      index += 8;
    } else break;
  }
  return found;
}

function zigzag(number) {
  const half = Math.floor(number / 2);
  return number % 2 ? -half - 1 : half;
}

function waarde(raw) {
  for (const [field, wire, value] of velden(raw)) {
    if (field === 1 && wire === 2) return new TextDecoder().decode(value);
    if (field === 7) return Boolean(value);
    if ([4, 5, 6].includes(field) && wire === 0) {
      let number = field === 6 ? zigzag(value) : value;
      if (number >= 2 ** 63) number -= 2 ** 64;
      return number;
    }
    if (field === 2 || field === 3) return null;
  }
  return null;
}

function packed(raw) {
  let index = 0;
  const numbers = [];
  while (index < raw.length) {
    const [number, next] = leesVarint(raw, index);
    index = next;
    numbers.push(number);
  }
  return numbers;
}

function ringen(nums) {
  let index = 0;
  let cursorX = 0;
  let cursorY = 0;
  const rings = [];
  let ring = [];
  while (index < nums.length) {
    const command = nums[index];
    index += 1;
    const kind = command & 7;
    const count = Math.floor(command / 8);
    if (kind === 1 || kind === 2) {
      if (kind === 1 && ring.length) {
        rings.push(ring);
        ring = [];
      }
      for (let step = 0; step < count; step += 1) {
        if (index + 1 >= nums.length) return rings;
        cursorX += zigzag(nums[index]);
        cursorY += zigzag(nums[index + 1]);
        index += 2;
        ring.push([cursorX, cursorY]);
      }
    } else if (kind === 7) {
      if (ring.length) rings.push(ring);
      ring = [];
    } else break;
  }
  if (ring.length) rings.push(ring);
  return rings;
}

function naarRd(zoom, tileX, tileY, ringsIn, extent) {
  const span = (3440.64 / 2 ** zoom) * 256;
  const west = ORIGIN_X + tileX * span;
  const north = ORIGIN_Y - tileY * span;
  return ringsIn.map((ring) => ring.map(([px, py]) => [west + (px / extent) * span, north - (py / extent) * span]));
}

function vlakkenUitTegel(blob, zoom, tileX, tileY, gekozen) {
  if (blob[0] === 0x1f && blob[1] === 0x8b) blob = gunzipSync(blob);
  const vlakken = [];
  for (const [field, wire, layer] of velden(blob)) {
    if (field !== 3 || wire !== 2) continue;
    const parts = velden(layer);
    const name = parts.find(([number, kind]) => number === 1 && kind === 2)?.[2];
    if (!name || new TextDecoder().decode(name) !== "planobject_polygon") continue;
    const keys = parts.filter(([number, kind]) => number === 3 && kind === 2).map(([, , value]) => new TextDecoder().decode(value));
    const values = parts.filter(([number, kind]) => number === 4 && kind === 2).map(([, , value]) => waarde(value));
    const extent = parts.find(([number, kind]) => number === 5 && kind === 0)?.[2] || 4096;
    for (const feature of parts.filter(([number, kind]) => number === 2 && kind === 2).map(([, , value]) => value)) {
      let tags = [];
      let geometry = [];
      for (const [number, kind, value] of velden(feature)) {
        if (number === 2 && kind === 2) tags = packed(value);
        else if (number === 4 && kind === 2) geometry = packed(value);
        else if (number === 2 && kind === 0) tags.push(value);
      }
      const props = {};
      for (let offset = 0; offset < tags.length - 1; offset += 2) {
        if (tags[offset] < keys.length && tags[offset + 1] < values.length) props[keys[tags[offset]]] = values[tags[offset + 1]];
      }
      const naam = String(props.naam || props.classificatie || "");
      if (!pastBij(naam, gekozen)) continue;
      const ringsOut = naarRd(zoom, tileX, tileY, ringen(geometry), Number(extent || 4096));
      if (ringsOut.length) {
        vlakken.push({
          naam,
          categorie: String(props.categorie || props.type || ""),
          plan: String(props.planid || ""),
          ringen: ringsOut,
        });
      }
    }
  }
  return vlakken;
}

async function tegel(planId, zoom, tileX, tileY, gekozen) {
  const [status, raw] = await fetchBytes(`${TILES}/${encodeURIComponent(planId)}/tiles/${zoom}/${tileX}/${tileY}.mvt`, 20000);
  if (status !== 200 || !raw.length) return [];
  try {
    return vlakkenUitTegel(raw, zoom, tileX, tileY, gekozen);
  } catch {
    return [];
  }
}

async function planVak(planId) {
  const [status, data] = await fetchJson(`${TILES}/${encodeURIComponent(planId)}/`, 20000);
  if (status !== 200 || !data?.bounds) return null;
  const bounds = String(data.bounds).split(",").map(Number);
  if (bounds.length !== 4 || bounds.some((value) => !Number.isFinite(value))) return null;
  const xs = [];
  const ys = [];
  for (const lon of [bounds[0], bounds[2]]) {
    for (const lat of [bounds[1], bounds[3]]) {
      const [x, y] = wgsNaarRd(lat, lon);
      xs.push(x);
      ys.push(y);
    }
  }
  return { minx: Math.min(...xs), miny: Math.min(...ys), maxx: Math.max(...xs), maxy: Math.max(...ys) };
}

function doorsnede(links, rechts) {
  const vak = {
    minx: Math.max(links.minx, rechts.minx),
    miny: Math.max(links.miny, rechts.miny),
    maxx: Math.min(links.maxx, rechts.maxx),
    maxy: Math.min(links.maxy, rechts.maxy),
  };
  if (vak.minx >= vak.maxx || vak.miny >= vak.maxy) return null;
  return vak;
}

function spreid(tegels, limiet) {
  if (tegels.length <= limiet) return tegels;
  const xs = [...new Set(tegels.map(([x]) => x))].sort((a, b) => a - b);
  const ys = [...new Set(tegels.map(([, y]) => y))].sort((a, b) => a - b);
  const kolommen = Math.max(1, Math.round(Math.sqrt(limiet * Math.max(xs.length, 1) / Math.max(ys.length, 1))));
  const rijen = Math.max(1, Math.ceil(limiet / kolommen));
  const gekozen = [];
  const gezien = new Set();
  for (let kolom = 0; kolom < kolommen; kolom += 1) {
    for (let rij = 0; rij < rijen; rij += 1) {
      const x = xs[Math.min(xs.length - 1, Math.floor((kolom + 0.5) * xs.length / kolommen))];
      const y = ys[Math.min(ys.length - 1, Math.floor((rij + 0.5) * ys.length / rijen))];
      const sleutel = `${x},${y}`;
      if (!gezien.has(sleutel)) {
        gezien.add(sleutel);
        gekozen.push([x, y]);
      }
    }
  }
  return gekozen.slice(0, limiet);
}

function zelfdeGemeente(planId, code) {
  const planCode = String(planId).split(".")[2] || "";
  if (!code || !planCode) return true;
  return planCode === code || planCode.startsWith("99");
}

async function vlakken(planIds, vak, gekozen, volledig = new Set()) {
  const grenzen = await pool(planIds, 8, planVak);
  const taken = [];
  planIds.forEach((planId, index) => {
    if (!zelfdeGemeente(planId, vak.code || "") && !volledig.has(planId)) return;
    const stuk = grenzen[index] ? doorsnede(vak, grenzen[index]) : null;
    if (!stuk) return;
    const span = Math.max(stuk.maxx - stuk.minx, stuk.maxy - stuk.miny);
    const zoom = span <= 1800 ? 11 : zoomVoor(stuk);
    const tegels = tegelBereik(stuk, zoom);
    const limiet = vak.provincie
      ? Math.min(tegels.length, 8)
      : (volledig.has(planId)
        ? Math.min(tegels.length, 40)
        : (tegels.length <= 20 ? tegels.length : (span > 8000 ? 8 : 20)));
    for (const [x, y] of spreid(tegels, limiet)) taken.push([planId, zoom, x, y]);
  });
  const batches = await pool(taken, 12, ([planId, zoom, x, y]) => tegel(planId, zoom, x, y, gekozen));
  const uniek = new Map();
  for (const vlak of batches.flat()) {
    const sleutel = `${vlak.plan}|${vlak.naam}|${Math.round(vlak.ringen[0][0][0])}|${Math.round(vlak.ringen[0][0][1])}`;
    uniek.set(sleutel, vlak);
  }
  return [...uniek.values()];
}

function binnen(x, y, ringsIn) {
  let geraakt = false;
  for (const ring of ringsIn) {
    let vorige = ring.length - 1;
    ring.forEach(([xi, yi], index) => {
      const [xj, yj] = ring[vorige];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / ((yj - yi) || 1e-12) + xi) geraakt = !geraakt;
      vorige = index;
    });
  }
  return geraakt;
}

function overlapt(vlak, vak) {
  const xs = vlak.ringen.flatMap((ring) => ring.map(([x]) => x));
  const ys = vlak.ringen.flatMap((ring) => ring.map(([, y]) => y));
  if (!xs.length) return false;
  return !(Math.max(...xs) < vak.minx || Math.min(...xs) > vak.maxx || Math.max(...ys) < vak.miny || Math.min(...ys) > vak.maxy);
}

function adresTekst(props) {
  const nummer = String(props.huisnummer || "");
  const extra = `${props.huisletter || ""}${props.toevoeging ? `-${props.toevoeging}` : ""}`;
  const postcode = String(props.postcode || "").replace(/\s/g, "");
  return `${props.openbare_ruimte_naam || ""} ${nummer}${extra}, ${postcode} ${props.woonplaats_naam || ""}`.replace(/\s+/g, " ").replace(/^[ ,]+|[ ,]+$/g, "");
}

function afstand(x, y, ringsIn) {
  let best = 1e18;
  for (const ring of ringsIn) {
    ring.forEach(([x1, y1], index) => {
      const [x2, y2] = ring[(index + 1) % ring.length];
      const dx = x2 - x1;
      const dy = y2 - y1;
      const lengte = dx * dx + dy * dy || 1;
      const t = Math.max(0, Math.min(1, ((x - x1) * dx + (y - y1) * dy) / lengte));
      const px = x1 + t * dx;
      const py = y1 + t * dy;
      best = Math.min(best, (x - px) ** 2 + (y - py) ** 2);
    });
  }
  return Math.sqrt(best);
}

async function bagInVak(minx, miny, maxx, maxy, paginas) {
  const [zuid, west] = rdNaarWgs(minx, miny);
  const [noord, oost] = rdNaarWgs(maxx, maxy);
  let url = `${BAG}?${new URLSearchParams({
    f: "json",
    limit: "200",
    bbox: `${Math.min(west, oost)},${Math.min(zuid, noord)},${Math.max(west, oost)},${Math.max(zuid, noord)}`,
  }).toString().replace(/%2C/g, ",")}`;
  const features = [];
  for (let page = 0; page < paginas; page += 1) {
    const [status, data] = await fetchJson(url, 30000);
    if (status !== 200 || !data) break;
    const batch = data.features || [];
    features.push(...batch);
    const volgende = (data.links || []).find((link) => link.rel === "next")?.href || "";
    if (batch.length < 200 || !volgende) break;
    url = volgende;
  }
  return features;
}

async function adressenVanVlak(vlak, vak) {
  const xs = vlak.ringen.flatMap((ring) => ring.map(([x]) => x));
  const ys = vlak.ringen.flatMap((ring) => ring.map(([, y]) => y));
  if (!xs.length) return [];
  const groot = Math.max(...xs) - Math.min(...xs) > 700 || Math.max(...ys) - Math.min(...ys) > 700;
  const marge = groot ? 0 : 20;
  const minx = Math.max(Math.min(...xs) - marge, vak.minx - marge);
  const maxx = Math.min(Math.max(...xs) + marge, vak.maxx + marge);
  const miny = Math.max(Math.min(...ys) - marge, vak.miny - marge);
  const maxy = Math.min(Math.max(...ys) + marge, vak.maxy + marge);
  if (minx >= maxx || miny >= maxy) return [];
  const gevonden = [];
  for (const feature of await bagInVak(minx, miny, maxx, maxy, groot ? 6 : 2)) {
    const coords = feature.geometry?.coordinates || [];
    if (coords.length < 2) continue;
    const [lon, lat] = coords;
    const [x, y] = wgsNaarRd(lat, lon);
    if (vak.straal && vak.cx != null && (x - vak.cx) ** 2 + (y - vak.cy) ** 2 > vak.straal ** 2) continue;
    if (!binnen(x, y, vlak.ringen) && (groot || afstand(x, y, vlak.ringen) > 12)) continue;
    const props = feature.properties || {};
    const bron = String(props.bronhouder_naam || "").trim().toLowerCase();
    if (bron && vak.gemeente && bron !== String(vak.gemeente).trim().toLowerCase()) continue;
    if (bron && vak.gemeenten && !vak.gemeenten.some((naam) => naam.trim().toLowerCase() === bron)) continue;
    const adres = adresTekst(props);
    if (!adres) continue;
    gevonden.push({
      adres,
      bestemming: vlak.naam,
      soort: vlak.categorie,
      plan: vlak.plan,
      lat: Math.round(lat * 1e6) / 1e6,
      lon: Math.round(lon * 1e6) / 1e6,
      gemeente: props.bronhouder_naam || "",
      woonplaats: props.woonplaats_naam || "",
    });
  }
  return gevonden;
}

async function adressen(vlakLijst, vak) {
  const resultaten = new Map();
  let afgekapt = false;
  const batches = await pool(vlakLijst.slice(0, 80), 8, (vlak) => adressenVanVlak(vlak, vak));
  for (const item of batches.flat()) {
    const bestaand = resultaten.get(item.adres);
    if (bestaand) {
      if (!bestaand.bestemming.includes(item.bestemming)) bestaand.bestemming += `, ${item.bestemming}`;
      continue;
    }
    if (resultaten.size >= MAX_MATCHES) {
      afgekapt = true;
      continue;
    }
    resultaten.set(item.adres, item);
  }
  return [[...resultaten.values()], afgekapt];
}

export async function zoek(params) {
  const gekozen = termen(params);
  const page = Math.max(1, Number.parseInt(params.page || "1", 10) || 1);
  const [vak, fout] = await gebied(params);
  if (fout) return { fout };
  const sleutel = JSON.stringify({ vak: Object.fromEntries(Object.entries(vak).map(([key, value]) => [key, typeof value === "number" ? Math.round(value * 10) / 10 : value])), termen: gekozen });
  const nu = Date.now();
  const gecached = cache.get(sleutel);
  let bundel = gecached && nu - gecached[0] < 600000 ? gecached[1] : null;
  if (!bundel) {
    try {
      let uitIndex;
      let planIds;
      let meer = false;
      if (vak.provincie) {
        const toegestaan = codesMetVoorganger(vak.codes);
        if (gekozen.includes("horeca") && gekozen.some((term) => term !== "horeca")) {
          [uitIndex, meer] = plannenVanProvincie(vak.codes, gekozen.filter((term) => term !== "horeca"), 16);
          const extra = (await plannen(vak, 5)).filter((planId) => planHoort(planId, toegestaan)).slice(0, 8);
          planIds = [...new Set([...uitIndex, ...extra])];
          meer = true;
        } else if (gekozen.includes("horeca")) {
          uitIndex = [];
          planIds = (await plannen(vak, 5)).filter((planId) => planHoort(planId, toegestaan)).slice(0, 16);
          meer = true;
        } else {
          [uitIndex, meer] = plannenVanProvincie(vak.codes, gekozen, 24);
          planIds = uitIndex;
        }
      } else {
        uitIndex = plannenUitIndex(vak.code || "", gekozen);
        planIds = [...new Set([...uitIndex, ...await plannen(vak)])];
      }
      const vlakLijst = (await vlakken(planIds, vak, gekozen, new Set(uitIndex))).filter((vlak) => overlapt(vlak, vak));
      const [matches, afgekaptVlak] = vlakLijst.length ? await adressen(vlakLijst, vak) : [[], false];
      const afgekapt = afgekaptVlak || vlakLijst.length > 80 || meer;
      matches.sort((a, b) => a.adres.localeCompare(b.adres, "nl"));
      bundel = { matches, afgekapt };
      if (cache.size > 12) cache.clear();
      cache.set(sleutel, [nu, bundel]);
    } catch {
      return { fout: "De bestemmingskaart of de BAG antwoordt niet." };
    }
  }
  const start = (page - 1) * PAGE_SIZE;
  return {
    totaal: bundel.matches.length,
    pagina: page,
    perPagina: PAGE_SIZE,
    afgekapt: bundel.afgekapt,
    gebied: vak.label,
    bestemming: gekozen,
    geenVlak: geenVlak(vak, gekozen),
    bron: "Vastgesteld bestemmingsplan via PDOK, halfjaarlijks bijgewerkt. Adressen uit de BAG.",
    resultaten: bundel.matches.slice(start, start + PAGE_SIZE),
  };
}
