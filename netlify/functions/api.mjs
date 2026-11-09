import { zoek as zoekPanden } from "./bestemming.mjs";

const SRU_URL = "https://repository.overheid.nl/sru";
const LOCATIE_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/suggest";
const LOCATIE_FREE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free";
const LOCATIE_REVERSE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse";
const BAG_URL = "https://api.pdok.nl/kadaster/bag/ogc/v2";
const BRK_URL = "https://api.pdok.nl/kadaster/brk-kadastrale-kaart/ogc/v1";
const USER_AGENT = "Scraper/0.2 (Netlify; officiële bekendmakingen, BAG en perceel)";
const PAGE_SIZE = 15;
const PROVINCIES = new Set([
  "Drenthe", "Flevoland", "Fryslân", "Gelderland", "Groningen", "Limburg",
  "Noord-Brabant", "Noord-Holland", "Overijssel", "Utrecht", "Zeeland", "Zuid-Holland",
]);
const GEBRUIKSDOEL = {
  woonfunctie: "wonen",
  bijeenkomstfunctie: "bijeenkomst",
  celfunctie: "cel",
  gezondheidszorgfunctie: "zorg",
  industriefunctie: "industrie",
  kantoorfunctie: "kantoor",
  logiesfunctie: "logies",
  onderwijsfunctie: "onderwijs",
  sportfunctie: "sport",
  winkelfunctie: "winkel",
  "overige gebruiksfunctie": "overig",
};
const RUBRIEKEN = {
  plan: "ruimtelijk plan of omgevingsdocument",
  vergunning: "omgevingsvergunning",
  exploitatie: "exploitatievergunning",
  melding: "exploitatiemelding",
};
const FASES = {
  ontwerp: "ontwerp inzage",
  vastgesteld: "vastgesteld verleend onherroepelijk",
};
const TITLE_ADDRESS = /([A-ZÀ-Ý][\w'.\-]*(?:\s+(?:[A-ZÀ-Ý][\w'.\-]*|van|de|den|der|het|ten|ter|op|aan)){0,6})\s+(\d{1,4}[A-Za-z]?(?:-\d{1,4})?)\s*,\s*(\d{4}\s?[A-Z]{2})(?:\s+((?:['’]s-)?[A-ZÀ-Ý][\w'.\-]+))?/u;

const provincieCache = new Map();
const puntCache = new Map();
const reverseCache = new Map();

function cqlTerm(value, limit = 80) {
  const cleaned = String(value || "").replace(/["\\<>]/g, "").replace(/\s+/g, " ").trim().slice(0, limit);
  if (!cleaned) return "";
  return /^[0-9A-Za-zÀ-ÿ]+$/u.test(cleaned) ? cleaned : `"${cleaned}"`;
}

function buildQuery(params, extra = []) {
  const parts = ["c.product-area==officielepublicaties"];
  const rubriek = params.rubriek || "plan";
  if (rubriek === "vroeg") {
    parts.push('(dt.type=="ruimtelijk plan of omgevingsdocument" OR dt.type=="omgevingsvergunning" OR dt.type=="exploitatievergunning")');
  } else if (RUBRIEKEN[rubriek]) {
    parts.push(`dt.type=="${RUBRIEKEN[rubriek]}"`);
  }
  const gemeente = cqlTerm(params.gemeente || "", 60);
  if (gemeente) parts.push(`(dt.creator=${gemeente} OR w.gemeentenaam=${gemeente})`);
  const woorden = cqlTerm(params.q || "", 80);
  if (woorden) parts.push(`cql.textAndIndexes all ${woorden}`);
  if (FASES[params.fase || ""]) parts.push(`cql.textAndIndexes any ${cqlTerm(FASES[params.fase])}`);
  if (/^\d{4}-\d{2}-\d{2}$/.test(params.vanaf || "")) parts.push(`dt.available>=${params.vanaf}`);
  parts.push(...extra);
  return `${parts.join(" AND ")} sortBy dt.available/sort.descending`;
}

async function getJson(url, timeout = 12000) {
  const response = await fetch(url, {
    headers: { "User-Agent": USER_AGENT, Accept: "application/json" },
    signal: AbortSignal.timeout(timeout),
  });
  if (!response.ok) {
    const error = new Error(`HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function getText(url, accept = "application/xml", timeout = 12000) {
  const response = await fetch(url, {
    headers: { "User-Agent": USER_AGENT, Accept: accept },
    signal: AbortSignal.timeout(timeout),
  });
  if (!response.ok) {
    const error = new Error(`HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.text();
}

async function locatieDocs(pairs) {
  const url = `${LOCATIE_FREE}?${new URLSearchParams(pairs)}`;
  const data = await getJson(url);
  return data.response?.docs || [];
}

async function gemeentenVanProvincie(provincie) {
  if (provincieCache.has(provincie)) return provincieCache.get(provincie);
  const docs = await locatieDocs([
    ["q", "*"],
    ["fq", "type:gemeente"],
    ["fq", `provincienaam:${provincie}`],
    ["rows", "80"],
    ["fl", "gemeentenaam"],
    ["sort", "gemeentenaam asc"],
  ]);
  const names = [];
  for (const doc of docs) {
    const name = String(doc.gemeentenaam || "").trim();
    if (name && !names.includes(name)) names.push(name);
  }
  provincieCache.set(provincie, names);
  return names;
}

function parsePunt(value) {
  const match = /POINT\(([0-9.]+)\s+([0-9.]+)\)/.exec(value || "");
  if (!match) return null;
  return [Number(match[2]), Number(match[1])];
}

async function geocode(plaats) {
  const key = plaats.toLocaleLowerCase("nl");
  if (puntCache.has(key)) return puntCache.get(key);
  for (const soort of ["woonplaats", "gemeente"]) {
    const docs = await locatieDocs([
      ["q", plaats],
      ["fq", `type:${soort}`],
      ["rows", "1"],
      ["fl", "centroide_ll"],
    ]);
    const punt = docs[0] ? parsePunt(docs[0].centroide_ll) : null;
    if (punt) {
      puntCache.set(key, punt);
      return punt;
    }
  }
  return null;
}

async function regionParts(params) {
  const extra = [];
  const provincie = String(params.provincie || "").trim();
  if (provincie) {
    if (!PROVINCIES.has(provincie)) return [[], "Kies een provincie uit de lijst."];
    const namen = await gemeentenVanProvincie(provincie);
    if (!namen.length) return [[], "Voor die provincie zijn geen gemeenten gevonden."];
    extra.push(`(${namen.map((naam) => `dt.creator=${cqlTerm(naam, 40)}`).join(" OR ")})`);
  }
  const plaats = String(params.plaats || "").replace(/\s+/g, " ").trim().slice(0, 60);
  if (plaats) {
    const ruw = params.straal || "5";
    if (!/^\d{1,2}$/.test(ruw) || Number(ruw) < 1 || Number(ruw) > 25) {
      return [[], "Kies een straal tussen 1 en 25 km."];
    }
    const punt = await geocode(plaats);
    if (!punt) return [[], "Die plaats is niet gevonden."];
    extra.push(`w.locatiepunt within/etrs89 "${punt[0].toFixed(5)} ${punt[1].toFixed(5)} ${Number(ruw).toFixed(3)}"`);
  }
  return [extra, null];
}

function decodeXml(value) {
  return value
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&");
}

function tagTexts(xml, name) {
  const re = new RegExp(`<(?:[\\w.-]+:)?${name}\\b[^>]*>([\\s\\S]*?)</(?:[\\w.-]+:)?${name}>`, "gi");
  const found = [];
  for (const match of xml.matchAll(re)) {
    const text = decodeXml(match[1].replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim());
    if (text && !found.includes(text)) found.push(text);
  }
  return found;
}

function firstTag(xml, name) {
  return tagTexts(xml, name)[0] || "";
}

function formatPostcode(value) {
  const compact = value.replace(/\s/g, "");
  return compact.length === 6 ? `${compact.slice(0, 4)} ${compact.slice(4)}` : value;
}

function addressFromTitle(title) {
  const match = TITLE_ADDRESS.exec(title);
  if (!match) return "";
  const tail = [formatPostcode(match[3]), match[4] || ""].filter(Boolean).join(" ");
  return `${match[1]} ${match[2]}, ${tail}`;
}

function addressOf(record, title) {
  const street = firstTag(record, "straatnaam");
  const number = `${firstTag(record, "huisnummer")}${firstTag(record, "huisletter")}${firstTag(record, "huisnummertoevoeging")}`.trim();
  const line = [street, number].filter(Boolean).join(" ");
  if (line) {
    const postcode = firstTag(record, "postcode");
    const tail = [postcode ? formatPostcode(postcode) : "", firstTag(record, "woonplaats")].filter(Boolean).join(" ");
    return [line, tail].filter(Boolean).join(", ");
  }
  return addressFromTitle(title);
}

function coordinaten(record) {
  for (const value of tagTexts(record, "locatiepunt")) {
    const parts = value.trim().split(/[\s,]+/);
    const breedte = Number(parts[0]);
    const lengte = Number(parts[1]);
    if (Number.isFinite(breedte) && Number.isFinite(lengte) && breedte >= 50 && breedte <= 54 && lengte >= 3 && lengte <= 8) {
      return [breedte, lengte];
    }
  }
  return null;
}

function planUrl(planId) {
  return planId
    ? `https://omgevingswet.overheid.nl/regels-op-de-kaart/document?documentID=${encodeURIComponent(planId)}`
    : "";
}

function parseRecords(xml) {
  const meldingen = tagTexts(xml, "message");
  const totalText = firstTag(xml, "numberOfRecords");
  const resultaten = [];
  for (const chunk of xml.split(/<[^>]*\brecordData\b[^>]*>/i).slice(1)) {
    const record = chunk.split(/<\/[^>]*\brecordData\b>/i)[0];
    const titel = firstTag(record, "title");
    if (!titel) continue;
    const id = firstTag(record, "identifier");
    const punt = coordinaten(record);
    const planId = firstTag(record, "bekendmakingBetreffendePlan");
    resultaten.push({
      id,
      titel,
      type: firstTag(record, "type"),
      gemeente: firstTag(record, "creator"),
      datum: firstTag(record, "available") || firstTag(record, "date"),
      gewijzigd: firstTag(record, "modified"),
      reactieTot: firstTag(record, "datumEindeReactietermijn"),
      adres: addressOf(record, titel),
      adresBron: "",
      lat: punt ? punt[0] : null,
      lon: punt ? punt[1] : null,
      planId,
      planUrl: planUrl(planId),
      url: firstTag(record, "preferredUrl") || (id ? `https://zoek.officielebekendmakingen.nl/${id}.html` : ""),
      blad: firstTag(record, "publicatienaam"),
    });
  }
  const totaal = Number.parseInt(totalText, 10);
  return { totaal: Number.isFinite(totaal) ? totaal : resultaten.length, resultaten, meldingen };
}

async function dichtstbij(lat, lon) {
  const key = `${lat.toFixed(5)},${lon.toFixed(5)}`;
  if (reverseCache.has(key)) return reverseCache.get(key);
  const url = `${LOCATIE_REVERSE}?${new URLSearchParams({
    lat: lat.toFixed(6),
    lon: lon.toFixed(6),
    type: "adres",
    rows: "1",
  })}`;
  const data = await getJson(url, 8000);
  const naam = data.response?.docs?.[0]?.weergavenaam || "";
  if (reverseCache.size > 400) reverseCache.clear();
  reverseCache.set(key, naam);
  return naam;
}

async function vulAdressen(items) {
  await Promise.all(items.filter((item) => !item.adres && item.lat != null).map(async (item) => {
    try {
      const gevonden = await dichtstbij(item.lat, item.lon);
      if (gevonden) {
        item.adres = gevonden;
        item.adresBron = "pdok";
      }
    } catch {
      /* Een ontbrekend kaartadres laat de publicatie staan. */
    }
  }));
}

export async function search(params) {
  const page = Math.max(1, Number.parseInt(params.page || "1", 10) || 1);
  let extra;
  let regionFout;
  try {
    [extra, regionFout] = await regionParts(params);
  } catch {
    return { fout: "De plaats of provincie kon niet worden opgezocht.", query: "" };
  }
  if (regionFout) return { fout: regionFout, query: "" };
  const query = buildQuery(params, extra);
  const url = `${SRU_URL}?${new URLSearchParams({
    query,
    startRecord: String((page - 1) * PAGE_SIZE + 1),
    maximumRecords: String(PAGE_SIZE),
    httpAccept: "application/xml",
  })}`;
  let xml;
  try {
    xml = await getText(url);
  } catch (error) {
    const status = error.status;
    return {
      fout: status ? `De zoekdienst antwoordde met ${status}.` : "Officiële Bekendmakingen is nu niet bereikbaar.",
      query,
    };
  }
  const parsed = parseRecords(xml);
  await vulAdressen(parsed.resultaten);
  const alleenAdres = params.adres === "1";
  const resultaten = alleenAdres ? parsed.resultaten.filter((item) => item.adres) : parsed.resultaten;
  return {
    totaal: parsed.totaal,
    pagina: page,
    perPagina: PAGE_SIZE,
    resultaten,
    query,
    meldingen: parsed.meldingen,
    adresFilter: alleenAdres,
  };
}

export async function gemeenten(term) {
  const cleaned = cqlTerm(term, 40).replaceAll('"', "");
  if (cleaned.length < 2) return { gemeenten: [] };
  try {
    const data = await getJson(`${LOCATIE_URL}?${new URLSearchParams({ q: cleaned, fq: "type:gemeente", rows: "8" })}`);
    const names = [];
    for (const doc of data.response?.docs || []) {
      const short = String(doc.weergavenaam || "").replace(/^Gemeente\s+/, "").trim();
      if (short && !names.includes(short)) names.push(short);
    }
    return { gemeenten: names };
  } catch {
    return { gemeenten: [] };
  }
}

function kiesAdres(docs, vraag) {
  const match = /(\d+)\s*([A-Za-z])?/.exec(vraag);
  if (!match) return docs[0];
  const nummer = match[1];
  const letter = (match[2] || "").toUpperCase();
  return docs.reduce((best, doc) => {
    const huis = String(doc.huisnummer || "");
    const extra = String(doc.huisletter || "").toUpperCase();
    let punten = huis === nummer ? 5 : 0;
    punten += letter ? (extra === letter ? 3 : -2) : (extra ? 0 : 3);
    return punten > best.punten ? { doc, punten } : best;
  }, { doc: docs[0], punten: -99 }).doc;
}

function jsonProps(payload) {
  if (payload.properties) return payload.properties;
  return payload.features?.[0]?.properties || {};
}

function note(field, reason, detail) {
  return { field, reason, detail };
}

async function perceelVan(doc) {
  const raw = Array.isArray(doc.gekoppeld_perceel)
    ? doc.gekoppeld_perceel
    : (doc.gekoppeld_perceel ? [doc.gekoppeld_perceel] : []);
  for (const item of raw.slice(0, 3)) {
    const match = /^([A-Z0-9]{3,6})[-\s]+([A-Z]{1,2})[-\s]+(\d{1,5})$/.exec(String(item).toUpperCase());
    if (!match) continue;
    const nummer = String(Number.parseInt(match[3], 10));
    try {
      const data = await getJson(`${BRK_URL}/collections/perceel/items?${new URLSearchParams({
        f: "json",
        limit: "1",
        akr_kadastrale_gemeente_code_waarde: match[1],
        sectie: match[2],
        perceelnummer: nummer,
      })}`);
      const props = data.features?.[0]?.properties;
      if (!props) continue;
      const localId = props.identificatie_lokaal_id || "";
      const namespace = props.identificatie_namespace || "";
      return {
        parcel: [props.kadastrale_gemeente_waarde, props.sectie, props.perceelnummer].filter(Boolean).join(" "),
        parcel_id: [namespace, localId].filter(Boolean).join("."),
      };
    } catch {
      return { parcel: `${match[1]} ${match[2]} ${nummer}`, parcel_id: "" };
    }
  }
  return { parcel: "", parcel_id: "" };
}

async function rapport(doc) {
  const ownership = {
    owner_name: "",
    owner_type: "",
    kvk_number: "",
    parcel: "",
    parcel_id: "",
    right: "",
    source: "",
  };
  const contact = {
    company_name: "",
    website: "",
    email: "",
    phone: "",
    business_address: "",
    contact_person: "",
  };
  const parcel = await perceelVan(doc);
  ownership.parcel = parcel.parcel;
  ownership.parcel_id = parcel.parcel_id;
  const unavailable = [
    note("ownership.owner_name", "Authentication required.", "Zet KADASTER_API_KEY. Zonder sleutel is de tenaamstelling niet op te vragen."),
    note(
      "ownership.owner_name",
      "Paid subscription required.",
      "De open PDOK-kadastrale kaart bevat geen eigenaar. Objectinformatie product 'rechten' kost € 2,96 per adres via Mijn Kadaster met Kadata Internet.",
    ),
    note("contact_information.email", "Authentication required.", "Zonder eigenaar is niet te bepalen welke organisatie moet worden gezocht."),
  ];
  if (!ownership.parcel) {
    unavailable.unshift(note("ownership.parcel", "Information not publicly available.", "PDOK Locatieserver en de kadastrale kaart gaven geen perceel bij dit adres."));
  }
  return {
    ownership,
    contact_information: contact,
    contact_source: "",
    contact_verified: false,
    unavailable,
  };
}

export async function pand(adres) {
  const cleaned = String(adres || "").replace(/\s+/g, " ").trim().slice(0, 120);
  if (cleaned.length < 5) return { fout: "Geen bruikbaar adres." };
  let docs;
  try {
    docs = await locatieDocs([["q", cleaned], ["fq", "type:adres"], ["rows", "8"]]);
  } catch {
    return { fout: "Het adres kon niet worden opgezocht." };
  }
  if (!docs.length) return { fout: "Geen BAG-adres gevonden." };
  const doc = kiesAdres(docs, cleaned);
  const identificatie = String(doc.adresseerbaarobject_id || "");
  if (!/^\d{14,16}$/.test(identificatie)) return { fout: "Dit adres heeft geen verblijfsobject." };
  let features = [];
  try {
    const data = await getJson(`${BAG_URL}/collections/verblijfsobject/items?f=json&identificatie=${identificatie}&limit=5`);
    features = data.features || [];
  } catch {
    features = [];
  }
  const gekozen = features.slice().sort((a, b) => {
    const status = String(a.properties?.status || "").toLowerCase();
    const other = String(b.properties?.status || "").toLowerCase();
    return (status.includes("in gebruik") ? 0 : 1) - (other.includes("in gebruik") ? 0 : 1);
  })[0];
  const props = gekozen?.properties || {};
  const ruweDoelen = props.gebruiksdoel || "";
  const doelen = Array.isArray(ruweDoelen)
    ? ruweDoelen.map(String)
    : String(ruweDoelen).split(",").map((item) => item.trim()).filter(Boolean);
  let bouwjaar = null;
  const hrefs = Array.isArray(props["pand.href"]) ? props["pand.href"] : (props["pand.href"] ? [props["pand.href"]] : []);
  if (hrefs[0] && String(hrefs[0]).startsWith(`${BAG_URL}/`)) {
    try {
      const scheiding = hrefs[0].includes("?") ? "&" : "?";
      bouwjaar = jsonProps(await getJson(`${hrefs[0]}${scheiding}f=json`)).bouwjaar ?? null;
    } catch {
      bouwjaar = null;
    }
  }
  const punt = parsePunt(doc.centroide_ll || "");
  const perceel = Array.isArray(doc.gekoppeld_perceel)
    ? doc.gekoppeld_perceel.slice(0, 3).join(", ")
    : (doc.gekoppeld_perceel || "");
  const resultaat = {
    adres: doc.weergavenaam || cleaned,
    lat: punt ? punt[0] : null,
    lon: punt ? punt[1] : null,
    oppervlakte: props.oppervlakte,
    gebruiksdoelen: doelen.map((item) => GEBRUIKSDOEL[item] || item),
    bouwjaar,
    perceel,
    gemeente: doc.gemeentenaam || "",
    woonplaats: doc.woonplaatsnaam || "",
  };
  try {
    Object.assign(resultaat, await rapport(doc));
  } catch {
    Object.assign(resultaat, {
      ownership: { owner_name: "", owner_type: "", kvk_number: "", parcel: "", parcel_id: "", right: "", source: "" },
      contact_information: { company_name: "", website: "", email: "", phone: "", business_address: "", contact_person: "" },
      contact_source: "",
      contact_verified: false,
      unavailable: [note("contact_information", "API does not provide this information.", "De contactkoppeling stopte door een onverwachte fout.")],
    });
  }
  return resultaat;
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

export const config = { path: "/api/*" };

export default async (request) => {
  const url = new URL(request.url);
  const params = Object.fromEntries(url.searchParams.entries());
  try {
    if (url.pathname === "/api/zoek" || url.pathname.endsWith("/zoek")) return jsonResponse(await search(params));
    if (url.pathname === "/api/gemeenten" || url.pathname.endsWith("/gemeenten")) return jsonResponse(await gemeenten(params.q || ""));
    if (url.pathname === "/api/panden" || url.pathname.endsWith("/panden")) return jsonResponse(await zoekPanden(params));
    if (url.pathname === "/api/pand" || url.pathname.endsWith("/pand")) return jsonResponse(await pand(params.adres || ""));
    return jsonResponse({ fout: "Niet gevonden." }, 404);
  } catch (error) {
    return jsonResponse({ fout: "De zoekserver kon dit verzoek niet afronden." }, 500);
  }
};
