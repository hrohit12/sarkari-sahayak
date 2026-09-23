"""Sarkari Sahayak - core engine.

Design rule: the LLM (optional) only UNDERSTANDS the citizen's words.
Eligibility, checklists and PDFs come from deterministic code + schemes.json,
so the agent can never invent a benefit amount or an eligibility rule.

Everything listed in TOOLS at the bottom is used by BOTH the web app (server.py)
and the MCP server (mcp_server.py), so there is one source of truth.
"""
import json
import os
import re
import urllib.request
from datetime import date
from pathlib import Path

BASE = Path(__file__).parent
DATA = json.loads((BASE / "schemes.json").read_text(encoding="utf-8"))
SCHEMES = {s["id"]: s for s in DATA["schemes"]}
FIELDS = DATA["fields"]
LANGS = ("en", "mr", "hi")
STATUS_ORDER = {"eligible": 0, "maybe": 1, "not_eligible": 2}
# derived fields are computed by normalize(); to ask about them we ask their sources
DERIVED_SOURCES = {"household_poor": ["ration_card"]}


def L(d, lang):
    """Pick a language from a {en, mr, hi} dict, falling back to English."""
    if isinstance(d, dict):
        return d.get(lang) or d.get("en", "")
    return d


# ----------------------------------------------------------------------------
# 1. Profile normalisation
# ----------------------------------------------------------------------------
def _to_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("true", "yes", "y", "1", "हो", "हाँ", "हां", "होय", "आहे", "है"):
        return True
    if s in ("false", "no", "n", "0", "नाही", "नहीं", "नही", "नाहीं"):
        return False
    return None


def normalize(profile):
    p = {k: v for k, v in (profile or {}).items() if v not in (None, "")}
    p.setdefault("resident_mh", True)  # demo is for Maharashtra; user can untick
    for k in ("age", "family_income", "residency_years"):
        if k in p:
            try:
                p[k] = int(float(p[k]))
            except (TypeError, ValueError):
                p.pop(k)
    for k, meta in FIELDS.items():
        if meta["type"] == "bool" and k in p:
            b = _to_bool(p[k])
            p.pop(k) if b is None else p.__setitem__(k, b)
    if "resident_mh" in p:
        p["resident_mh"] = bool(_to_bool(p["resident_mh"]))
    if p.get("special_status") == "widow":
        p.setdefault("gender", "female")
    # Namo Shetkari needs PM-KISAN, which needs land in the family
    if p.get("land_owner") is False:
        p.setdefault("pm_kisan_beneficiary", False)
    # derived: poor household (for Ujjwala)
    rc = p.get("ration_card")
    if rc in ("yellow", "aay") or p.get("bpl") is True:
        p["household_poor"] = True
    elif rc in ("orange", "white", "none"):
        p["household_poor"] = False
    return p


# ----------------------------------------------------------------------------
# 2. Deterministic rule evaluation
# ----------------------------------------------------------------------------
def _eval_rule(r, p):
    """Return (state, missing_fields); state is pass | fail | unknown."""
    if r["op"] == "or":
        res = [_eval_rule(x, p) for x in r["of"]]
        if any(s == "pass" for s, _ in res):
            return "pass", []
        if all(s == "fail" for s, _ in res):
            return "fail", []
        return "unknown", [m for s, ms in res if s == "unknown" for m in ms]
    f, v = r["f"], p.get(r["f"])
    if v is None:
        return "unknown", [f]
    op, x = r["op"], r["v"]
    ok = {
        "eq": lambda: v == x,
        "gte": lambda: v >= x,
        "lte": lambda: v <= x,
        "between": lambda: x[0] <= v <= x[1],
        "in": lambda: v in x,
    }[op]()
    return ("pass" if ok else "fail"), []


def _option_label(field, value, lang):
    for o in FIELDS.get(field, {}).get("options", []):
        if o["value"] == value:
            return L(o["label"], lang)
    return str(value)


SUMMARY = {
    "en": "Based on what you told me: {a} scheme(s) look like a match, {b} need one or two more answers, {c} do not match.",
    "mr": "तुम्ही दिलेल्या माहितीनुसार: {a} योजना लागू दिसतात, {b} साठी आणखी एक-दोन उत्तरे हवीत, {c} लागू होत नाहीत.",
    "hi": "आपकी दी हुई जानकारी के अनुसार: {a} योजनाएँ लागू दिखती हैं, {b} के लिए एक-दो उत्तर और चाहिए, {c} लागू नहीं हैं।",
}


def check_eligibility(profile: dict, lang: str = "en") -> dict:
    """Check a citizen profile against every scheme's rules. Returns status per scheme (eligible / maybe / not_eligible),
    which rules pass or fail, documents needed, where to apply, and the follow-up questions worth asking next.
    Profile keys: gender, age, family_income (annual Rs), residency_years, ration_card, land_owner, is_student ... (see schemes.json fields)."""
    p = normalize(profile)
    results, weight = [], {}
    for s in DATA["schemes"]:
        rules_out, hard, soft, unknown, missing = [], False, False, False, []
        for r in s["rules"]:
            state, miss = _eval_rule(r, p)
            if state == "fail" and r.get("soft"):
                soft = True
            elif state == "fail":
                hard = True
            elif state == "unknown":
                unknown = True
                missing += miss
            rules_out.append({"label": L(r["label"], lang), "state": state, "soft": bool(r.get("soft"))})
        status = "not_eligible" if hard else ("maybe" if (unknown or soft) else "eligible")
        need = []
        for m in dict.fromkeys(missing):
            for src in DERIVED_SOURCES.get(m, [m]):
                if src not in need and src != "resident_mh":
                    need.append(src)
        if status == "maybe":
            for m in need:
                weight[m] = weight.get(m, 0) + 1
        results.append({
            "id": s["id"], "level": s["level"], "name": L(s["name"], lang), "short": L(s["short"], lang),
            "benefit": L(s["benefit"], lang), "status": status,
            "apply_status": s["apply_status"], "status_note": L(s["status_note"], lang),
            "rules": rules_out, "missing": need,
            "documents": [L(DATA["documents"][d], lang) for d in s["docs"]],
            "apply": {"mode": s["apply"]["mode"], "where": L(s["apply"]["where"], lang), "url": s["apply"]["url"]},
            "caveat": L(s["caveat"], lang), "sources": s["sources"], "confidence": s["confidence"],
        })
    results.sort(key=lambda r: STATUS_ORDER[r["status"]])  # stable sort keeps file order inside a group
    order = list(FIELDS)
    asked = sorted(weight, key=lambda f: (-weight[f], order.index(f) if f in order else 99))[:5]
    questions = [{
        "field": f, "type": FIELDS[f]["type"], "q": L(FIELDS[f]["q"], lang),
        "options": [{"value": o["value"], "label": L(o["label"], lang)} for o in FIELDS[f].get("options", [])],
    } for f in asked if f in FIELDS]
    n = lambda st: sum(1 for r in results if r["status"] == st)
    return {
        "lang": lang, "profile": p, "results": results, "questions": questions,
        "summary": SUMMARY[lang].format(a=n("eligible"), b=n("maybe"), c=n("not_eligible")),
        "option_labels": {f: {o["value"]: L(o["label"], lang) for o in m["options"]} for f, m in FIELDS.items() if "options" in m},
        "verified_on": DATA["meta"]["verified_on"], "disclaimer": L(DATA["meta"]["disclaimer"], lang),
    }


def list_schemes(lang: str = "en") -> list:
    """List the Maharashtra/central welfare schemes in the knowledge base (lang: en | mr | hi)."""
    return [{"id": s["id"], "name": L(s["name"], lang), "level": s["level"], "benefit": L(s["benefit"], lang),
             "apply_status": s["apply_status"], "verified_on": DATA["meta"]["verified_on"], "sources": s["sources"]}
            for s in DATA["schemes"]]


def get_document_checklist(scheme_id: str, lang: str = "en") -> dict:
    """Documents to collect, where to apply and caveats for one scheme id (e.g. shravan_bal, pm_kisan)."""
    s = SCHEMES[scheme_id]
    return {"scheme_id": scheme_id, "scheme": L(s["name"], lang),
            "documents": [L(DATA["documents"][d], lang) for d in s["docs"]],
            "where_to_apply": L(s["apply"]["where"], lang), "url": s["apply"]["url"],
            "caveat": L(s["caveat"], lang)}


# ----------------------------------------------------------------------------
# 3. Understanding the citizen's words: rules (offline) + optional LLM
# ----------------------------------------------------------------------------
_DEV = str.maketrans("०१२३४५६७८९", "0123456789")
W = r"[^\s\d.,;:!?()]*"  # "rest of a word" - \w would drop Devanagari vowel signs
NEG = r"\bno\b|\bnot\b|don'?t|doesn'?t|never|nobody|नाही|नाहीत|नहीं|नही"


def _near(low, m, span=40):
    """The sentence (or clause) around a match - negations are judged inside it only."""
    a = max([low.rfind(c, 0, m.start()) for c in ".।!?\n"] + [-1]) + 1
    ends = [i for i in (low.find(c, m.end()) for c in ".।!?\n") if i != -1]
    return low[a: min(ends) if ends else len(low)]


def _money(win, ctx=None):
    """Find an amount such as '2 lakh', '15,000', '₹ 1.5 लाख' in a text window -> rupees or None."""
    for m in re.finditer(r"(?:₹|rs\.?|inr|रुपये|रु\.?)?\s*(\d[\d,]*(?:\.\d+)?)\s*(lakhs?|lacs?|लाख|thousand|हजार|हज़ार|crore|करोड)?", win):
        try:
            val = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        unit = m.group(2) or ""
        mult = 100000 if unit.startswith(("lakh", "lac", "लाख")) else 1000 if unit in ("thousand", "हजार", "हज़ार") else 10000000 if unit in ("crore", "करोड") else 1
        if mult == 1 and val < 1000:
            continue  # a bare small number is probably not money
        amount = val * mult
        if re.search(r"per month|monthly|a month|/month|महिना|महिन्याला|दरमहा|मासिक|महीना|महीने|प्रति माह", ctx or win):
            amount *= 12
        return int(amount)
    return None


def extract_rules(text):
    """Keyword/regex understanding of Marathi / Hindi / English. Only returns facts that are stated."""
    t = text.translate(_DEV)
    low = t.lower()
    p = {}

    # years lived in Maharashtra (removed from text before reading age)
    for pat in (rf"(?:maharashtra|महाराष्ट्र{W})\D{{0,25}}?(\d{{1,3}})\s*(?:years?|yrs?|वर्ष{W}|साल)",
                rf"(\d{{1,3}})\s*(?:years?|yrs?|वर्ष{W}|साल)\s*(?:से|पासून|पासुन)?\s*(?:\S+\s+){{0,2}}?(?:maharashtra|महाराष्ट्र)"):
        m = re.search(pat, low)
        if m:
            p["residency_years"] = int(m.group(1))
            low = low[:m.start()] + " " + low[m.end():]
            break

    # gender
    if re.search(r"\bwoman\b|\bfemale\b|\blady\b|\bgirl\b|\bwidow\b|महिला|स्त्री|मुलगी|विधवा|औरत|लड़की|वर्षांची|साल की", low):
        p["gender"] = "female"
    elif re.search(r"\bman\b|\bmale\b|\bboy\b|पुरुष|आदमी|मुलगा|वर्षांचा|साल का|लड़का", low):
        p["gender"] = "male"

    # age
    for pat in (r"(?:\bage\b|\baged\b|वय|उम्र|आयु)(?!ु)\D{0,8}?(\d{1,3})", rf"(\d{{1,3}})\s*(?:years?[- ]old|year[- ]old|yrs? old|y/o|वर्ष{W}|साल|बरस)"):
        m = re.search(pat, low)
        if m and 0 < int(m.group(1)) < 121:
            p["age"] = int(m.group(1))
            break

    # annual family income
    for kw in re.finditer(r"income|उत्पन्न\S*|कमाई|आय(?!ु)|पगार|salary|earn\w*|कमवत\S*", low):
        ctx = low[max(0, kw.start() - 20): kw.end() + 70]
        amt = _money(low[kw.end(): kw.end() + 60], ctx) or _money(low[max(0, kw.start() - 30): kw.start()], ctx)
        if amt:
            p["family_income"] = amt
            break

    # ration card
    if re.search(r"ration|रेशन|राशन|शिधा", low):
        if re.search(r"no ration|without ration|(?:रेशन|राशन) कार्ड (?:नाही|नहीं)|शिधापत्रिका नाही", low):
            p["ration_card"] = "none"
        elif re.search(r"antyodaya|अंत्योदय|\baay\b", low):
            p["ration_card"] = "aay"
        elif re.search(r"yellow|पिवळ|पीला|पीली|पीले", low):
            p["ration_card"] = "yellow"
        elif re.search(r"orange|saffron|केशरी|केसरी|नारंगी", low):
            p["ration_card"] = "orange"
        elif re.search(r"white|पांढ|सफेद|सफ़ेद", low):
            p["ration_card"] = "white"

    # land
    if re.search(r"no land|landless|don'?t (?:own|have) (?:any )?(?:farm)?land|भूमिहीन|जमीन नाही|जमिन नाही|शेती नाही|शेतजमीन नाही|जमीन नहीं|खेत नहीं|\btenant|कुळ|भाडेकरू|बटाई", low):
        p["land_owner"] = False
    elif re.search(r"7\s*/\s*12|सातबारा|own(?:s)? (?:\d+(?:\.\d+)? )?(?:acres?|hectares?|land|farm)|have (?:\d+(?:\.\d+)? )?(?:acres?|hectares?|land)|\d+(?:\.\d+)?\s*(?:acres?|एकर|hectares?|हेक्टर)|शेतजमीन|जमीन आहे|जमिन आहे|जमीन है|खेत है|शेती आहे", low):
        p["land_owner"] = True

    # income tax / government job
    m = re.search(r"income tax|आयकर|इनकम ?टॅक्स|इनकम ?टैक्स|\bitr\b", low)
    if m:
        p["income_tax_payer"] = not re.search(NEG, _near(low, m))
    m = re.search(r"government (?:job|employee|servant|service)|govt\.? (?:job|employee|servant)|सरकारी (?:नोकरी|कर्मचारी|नौकर)|शासकीय (?:नोकरी|कर्मचारी)", low)
    if m:
        p["govt_employee"] = not re.search(NEG, _near(low, m))

    # PM-KISAN
    m = re.search(r"pm[\s-]*kisan|पीएम[\s-]*किसान|पी\.?\s*एम\.?\s*किसान|किसान सन्मान|किसान सम्मान", low)
    if m:
        win = _near(low, m)
        if re.search(NEG, win):
            p["pm_kisan_beneficiary"] = False
        elif re.search(r"\bget|getting|receiv|मिळते|मिळतात|मिलता|मिलती|मिलते|beneficiary|लाभार्थी|registered|नोंदणी|पंजीकृत", win):
            p["pm_kisan_beneficiary"] = True

    # BPL
    m = re.search(r"\bbpl\b|गरीबी रेषेखाली|गरिबी रेषेखाली|गरीबी रेखा|बीपीएल", low)
    if m:
        p["bpl"] = not re.search(NEG, _near(low, m))

    # special status
    if re.search(r"widow|विधवा", low):
        p["special_status"] = "widow"
    elif re.search(r"disab|handicap|divyang|दिव्यांग|अपंग|विकलांग", low):
        p["special_status"] = "disabled"
    elif re.search(r"orphan|अनाथ", low):
        p["special_status"] = "orphan"
    elif re.search(r"destitute|निराधार", low):
        p["special_status"] = "destitute"

    # pension
    if re.search(r"no pension|पेन्शन नाही|पेंशन नहीं|पेन्शन मिळत नाही|पेंशन नहीं मिलती", low):
        p["gets_other_pension"] = False
    elif re.search(r"(?:get|getting|receive|receiving) (?:a )?pension|पेन्शन मिळते|पेंशन मिलती", low):
        p["gets_other_pension"] = True

    # student / scholarship
    if re.search(r"not a student|विद्यार्थी नाही|छात्र नहीं", low):
        p["is_student"] = False
    elif re.search(r"student|विद्यार्थी|विद्यार्थिनी|छात्र|छात्रा|college|कॉलेज|महाविद्यालय|degree|diploma|engineering|b\.?tech|pursuing", low):
        p["is_student"] = True
    if re.search(r"\b(?:sc|st|obc|vjnt|vj|nt)\b|scheduled (?:caste|tribe)|अनुसूचित|ओबीसी|इतर मागास|भटक्या|विमुक्त", low.replace("s.c.", "sc")):
        p["admission_category"] = "other"
    elif re.search(r"sebc|एसईबीसी", low):
        p["admission_category"] = "sebc"
    elif re.search(r"open\s+(?:category|quota|seat|merit)|general\s+(?:category|merit|quota)|खुल्या|खुला|सर्वसाधारण|सामान्य श्रेणी|अनारक्षित", low):
        p["admission_category"] = "open"
    if re.search(r"no (?:other )?scholarship|not (?:getting|receiving|get) (?:any )?scholarship|शिष्यवृत्ती (?:मिळत )?नाही|छात्रवृत्ति (?:नहीं|नही)", low):
        p["gets_other_scholarship"] = False
    elif re.search(r"(?:getting|receiv\w+|\bget|\bgot) (?:a |an |another |other )?scholarship|शिष्यवृत्ती मिळते|छात्रवृत्ति मिलती", low):
        p["gets_other_scholarship"] = True

    # LPG
    if re.search(r"no (?:lpg|gas)|(?:without|don'?t have|do not have|not have) (?:an? )?(?:lpg|gas)|गॅस (?:कनेक्शन|जोडणी) नाही|गैस (?:कनेक्शन|सिलेंडर) नहीं|गॅस नाही|गैस नहीं|सिलिंडर नाही|सिलेंडर नहीं", low):
        p["has_lpg"] = False
    elif re.search(r"(?:have|has|got) (?:an? )?(?:lpg|gas) (?:connection|cylinder)|गॅस (?:कनेक्शन|जोडणी) आहे|गैस कनेक्शन है", low):
        p["has_lpg"] = True

    # bank
    if re.search(r"no bank|without (?:a )?bank|बँक खाते नाही|खाते नाही|बैंक खाता नहीं|खाता नहीं", low):
        p["bank_aadhaar_linked"] = False
    elif re.search(r"(bank|बँक|बैंक|खाते|खाता).{0,40}(aadhaar|aadhar|आधार)|(aadhaar|aadhar|आधार).{0,40}(bank|बँक|बैंक|खाते|खाता)", low):
        p["bank_aadhaar_linked"] = True

    # outside Maharashtra
    if re.search(r"outside maharashtra|other state|not from maharashtra|दुसऱ्या राज्य|दूसरे राज्य", low):
        p["resident_mh"] = False

    # contact details for the PDF
    m = re.search(r"(?<!\d)([6-9]\d{9})(?!\d)", t)
    if m:
        p["mobile"] = m.group(1)
    m = re.search(r"(?:my name is|name is|name:)\s+([A-Za-z][A-Za-z .]{1,40}?)(?:[,.]|\s+and\b|\s+i\b|$)|(?:माझे नाव|मेरा नाम)\s+([^\s,.।]+(?:\s+[^\s,.।]+){0,2}?)(?:[,.।]|\s+आहे|\s+है|$)", t, re.I)
    if m:
        p["name"] = (m.group(1) or m.group(2)).strip()
    return p


LLM_KEYS = {
    "gender": "female | male", "age": "integer years", "family_income": "annual family income in rupees (integer; convert lakh/thousand/monthly)",
    "residency_years": "integer years lived in Maharashtra", "bank_aadhaar_linked": "true/false own Aadhaar-linked bank account",
    "ration_card": "yellow | orange | white | aay | none", "bpl": "true/false", "land_owner": "true/false family owns farmland (7/12)",
    "income_tax_payer": "true/false anyone in family", "govt_employee": "true/false anyone in family, serving or retired",
    "pm_kisan_beneficiary": "true/false", "special_status": "widow | disabled | orphan | destitute | none",
    "gets_other_pension": "true/false", "is_student": "true/false", "admission_category": "open | sebc | other",
    "gets_other_scholarship": "true/false", "has_lpg": "true/false household already has LPG", "name": "string", "mobile": "10-digit string",
}


def extract_llm(text):
    """Optional: ask Gemini to read the message. Needs GEMINI_API_KEY. Returns dict or None."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    system = ("You read a citizen's message (Marathi, Hindi or English) and return ONE JSON object. "
              "Use only facts the user actually stated; omit a key (or use null) when not stated. Never guess. "
              "Allowed keys: " + json.dumps(LLM_KEYS) + ". Output JSON only, no prose.")
    model = os.environ.get("SAHAYAK_MODEL", "gemini-2.5-flash")
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.load(r)
    raw = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0)) if m else None


def _clean(d):
    """Keep only known keys with valid types/values (never trust model output blindly)."""
    out = {}
    for k, v in (d or {}).items():
        if v is None or k not in LLM_KEYS:
            continue
        meta = FIELDS.get(k)
        if k in ("name", "mobile"):
            out[k] = str(v)[:60]
        elif meta and meta["type"] == "int":
            try:
                out[k] = int(float(v))
            except (TypeError, ValueError):
                pass
        elif meta and meta["type"] == "bool":
            b = _to_bool(v)
            if b is not None:
                out[k] = b
        elif meta and meta["type"] == "choice" and v in [o["value"] for o in meta["options"]]:
            out[k] = v
    return out


def parse_citizen_text(text: str, lang: str = "en") -> dict:
    """Turn a citizen's free text (Marathi, Hindi or English) into a profile dict. Only facts the user stated are returned."""
    profile, method = extract_rules(text), "rules"
    try:
        llm = extract_llm(text)
        if llm is not None:
            profile.update(_clean(llm))
            method = "llm+rules"
    except Exception as e:  # network / key problems must never break the demo
        method = "rules (LLM unavailable: %s)" % type(e).__name__
    return {"profile": profile, "method": method}


# ----------------------------------------------------------------------------
# 4. Filled draft form (PDF, Marathi / Hindi / English)
# ----------------------------------------------------------------------------
T = {
    "title": {"en": "Application help sheet (DRAFT)", "mr": "अर्ज मदत पत्रक (मसुदा)", "hi": "आवेदन सहायता पत्र (मसौदा)"},
    "notice": {"en": "Prepared by Sarkari Sahayak from your answers. This is NOT an official government form - copy these details into the official form/portal, or hand this over as a covering letter.",
               "mr": "सरकारी सहायकने तुमच्या माहितीवरून तयार केले. हा अधिकृत सरकारी अर्ज नाही - ही माहिती अधिकृत अर्ज/पोर्टलवर भरा किंवा कार्यालयात जोडपत्र म्हणून द्या.",
               "hi": "सरकारी सहायक ने आपकी जानकारी से तैयार किया। यह आधिकारिक सरकारी फॉर्म नहीं है - यह जानकारी आधिकारिक फॉर्म/पोर्टल में भरें या कार्यालय में कवरिंग पत्र के रूप में दें।"},
    "scheme": {"en": "Scheme", "mr": "योजना", "hi": "योजना"},
    "benefit": {"en": "Benefit", "mr": "लाभ", "hi": "लाभ"},
    "applicant": {"en": "Applicant details", "mr": "अर्जदाराचा तपशील", "hi": "आवेदक का विवरण"},
    "name": {"en": "Name", "mr": "नाव", "hi": "नाम"}, "age": {"en": "Age", "mr": "वय", "hi": "आयु"},
    "gender": {"en": "Gender", "mr": "लिंग", "hi": "लिंग"}, "village": {"en": "Village / Town", "mr": "गाव / शहर", "hi": "गाँव / शहर"},
    "district": {"en": "District", "mr": "जिल्हा", "hi": "जिला"}, "mobile": {"en": "Mobile", "mr": "मोबाईल", "hi": "मोबाइल"},
    "income": {"en": "Annual family income", "mr": "कुटुंबाचे वार्षिक उत्पन्न", "hi": "पारिवारिक वार्षिक आय"},
    "ration": {"en": "Ration card", "mr": "शिधापत्रिका", "hi": "राशन कार्ड"},
    "aadhaar": {"en": "Aadhaar no. (fill by hand - never stored)", "mr": "आधार क्रमांक (हाताने भरा - साठवला जात नाही)", "hi": "आधार नंबर (हाथ से भरें - सहेजा नहीं जाता)"},
    "bank": {"en": "Bank account no. / IFSC (fill by hand)", "mr": "बँक खाते क्र. / IFSC (हाताने भरा)", "hi": "बैंक खाता सं. / IFSC (हाथ से भरें)"},
    "elig": {"en": "Eligibility check (from your answers)", "mr": "पात्रता तपासणी (तुमच्या माहितीनुसार)", "hi": "पात्रता जाँच (आपकी जानकारी के अनुसार)"},
    "pass": {"en": "Meets", "mr": "पूर्ण", "hi": "पूरा"}, "fail": {"en": "Does not meet", "mr": "पूर्ण नाही", "hi": "पूरा नहीं"},
    "unknown": {"en": "Confirm", "mr": "खात्री करा", "hi": "पुष्टि करें"},
    "docs": {"en": "Documents to attach", "mr": "जोडावयाची कागदपत्रे", "hi": "संलग्न करने के दस्तावेज़"},
    "where": {"en": "Where to apply", "mr": "अर्ज कुठे करावा", "hi": "आवेदन कहाँ करें"},
    "letter": {"en": "Covering letter", "mr": "जोडपत्र (अर्ज)", "hi": "कवरिंग पत्र"},
    "note": {"en": "Note", "mr": "टीप", "hi": "टिप्पणी"},
    "footer": {"en": "Scheme data verified from public sources on {v}. Rules change - confirm at the official portal/office. Generated on {d}. Runs locally; nothing is stored.",
               "mr": "योजनेची माहिती {v} रोजी सार्वजनिक स्रोतांवरून तपासली. नियम बदलू शकतात - अधिकृत पोर्टल/कार्यालयात खात्री करा. तयार केले: {d}. स्थानिक चालते; काहीही साठवले जात नाही.",
               "hi": "योजना की जानकारी {v} को सार्वजनिक स्रोतों से जाँची गई। नियम बदल सकते हैं - आधिकारिक पोर्टल/कार्यालय में पुष्टि करें। तैयार: {d}। स्थानीय रूप से चलता है; कुछ भी सहेजा नहीं जाता।"},
    "letter_body": {
        "en": "To,\n{to}\n\nSubject: Application for {scheme}\n\nRespected Sir/Madam,\nI, {name}, aged {age}, resident of {village}, {district}, Maharashtra, request you to kindly consider my application for {scheme}. The documents listed above are attached. I declare that the information given is true to the best of my knowledge.",
        "mr": "प्रति,\n{to}\n\nविषय: {scheme} अंतर्गत लाभ मिळणेबाबत अर्ज\n\nमहोदय/महोदया,\nमी {name}, वय {age} वर्षे, रा. {village}, जि. {district}, महाराष्ट्र, {scheme} अंतर्गत लाभासाठी अर्ज सादर करीत आहे. वर नमूद कागदपत्रे सोबत जोडली आहेत. मी दिलेली माहिती माझ्या माहितीनुसार खरी आहे.",
        "hi": "सेवा में,\n{to}\n\nविषय: {scheme} के अंतर्गत लाभ हेतु आवेदन\n\nमहोदय/महोदया,\nमैं {name}, आयु {age} वर्ष, निवासी {village}, जिला {district}, महाराष्ट्र, {scheme} के अंतर्गत लाभ हेतु आवेदन प्रस्तुत करता/करती हूँ। ऊपर सूचीबद्ध दस्तावेज़ संलग्न हैं। मेरे द्वारा दी गई जानकारी मेरी जानकारी में सत्य है।"},
    "sign": {"en": "Place: ________   Date: ________   Signature: ____________",
             "mr": "ठिकाण: ________   दिनांक: ________   सही: ____________",
             "hi": "स्थान: ________   दिनांक: ________   हस्ताक्षर: ____________"},
}


def build_pdf(scheme_id, profile, lang="en"):
    """Return PDF bytes: applicant details, eligibility check, document checklist, covering letter."""
    import logging
    from fpdf import FPDF  # imported here so the rest of the engine works without it
    logging.getLogger("fontTools").setLevel(logging.ERROR)  # keep font-subsetting chatter out of the logs
    s, p = SCHEMES[scheme_id], normalize(profile)
    res = next(r for r in check_eligibility(p, lang)["results"] if r["id"] == scheme_id)
    blank = "_" * 22
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_margins(15, 12, 15)
    pdf.add_font("Mukta", "", str(BASE / "fonts" / "Mukta-Regular.ttf"))
    pdf.add_font("Mukta", "B", str(BASE / "fonts" / "Mukta-Bold.ttf"))
    pdf.set_text_shaping(True)  # needed for correct Devanagari conjuncts
    pdf.add_page()
    nl = dict(new_x="LMARGIN", new_y="NEXT")

    def heading(txt):
        pdf.ln(1.5)
        pdf.set_font("Mukta", "B", 11.5)
        pdf.set_text_color(20, 90, 60)
        pdf.cell(0, 6.5, txt, border="B", **nl)
        pdf.set_text_color(30, 30, 30)
        pdf.set_font("Mukta", "", 10.5)
        pdf.ln(1)

    def row(label, value):
        pdf.set_font("Mukta", "B", 10.5)
        pdf.cell(66, 5.6, label)
        pdf.set_font("Mukta", "", 10.5)
        pdf.multi_cell(0, 5.6, str(value) if value not in (None, "") else blank, **nl)

    pdf.set_fill_color(20, 90, 60)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Mukta", "B", 15)
    pdf.cell(0, 11, "  " + L(T["title"], lang), fill=True, **nl)
    pdf.set_text_color(150, 60, 20)
    pdf.set_font("Mukta", "", 9)
    pdf.multi_cell(0, 4.6, L(T["notice"], lang), **nl)
    pdf.set_text_color(30, 30, 30)

    heading(L(T["scheme"], lang))
    pdf.set_font("Mukta", "B", 12)
    pdf.multi_cell(0, 6.5, L(s["name"], lang), **nl)
    pdf.set_font("Mukta", "", 10.5)
    pdf.multi_cell(0, 6, L(T["benefit"], lang) + ": " + L(s["benefit"], lang), **nl)

    heading(L(T["applicant"], lang))
    row(L(T["name"], lang), p.get("name"))
    row(L(T["age"], lang), p.get("age"))
    row(L(T["gender"], lang), _option_label("gender", p["gender"], lang) if p.get("gender") else None)
    row(L(T["village"], lang), p.get("village"))
    row(L(T["district"], lang), p.get("district"))
    row(L(T["mobile"], lang), p.get("mobile"))
    row(L(T["income"], lang), "₹{:,}".format(p["family_income"]) if p.get("family_income") is not None else None)
    row(L(T["ration"], lang), _option_label("ration_card", p["ration_card"], lang) if p.get("ration_card") else None)
    row(L(T["aadhaar"], lang), None)
    row(L(T["bank"], lang), None)

    heading(L(T["elig"], lang))
    colors = {"pass": (20, 120, 60), "fail": (190, 40, 40), "unknown": (190, 120, 10)}
    for r in res["rules"]:
        pdf.set_text_color(*colors[r["state"]])
        pdf.set_font("Mukta", "B", 10)
        pdf.cell(28, 5.4, L(T[r["state"]], lang))
        pdf.set_text_color(30, 30, 30)
        pdf.set_font("Mukta", "", 10)
        pdf.multi_cell(0, 5.4, r["label"], **nl)

    heading(L(T["docs"], lang))
    for d in res["documents"]:
        x, y = pdf.get_x(), pdf.get_y()
        pdf.rect(x, y + 1.3, 3.4, 3.4)
        pdf.set_x(x + 6.5)
        pdf.multi_cell(0, 5.4, d, **nl)

    heading(L(T["where"], lang))
    pdf.multi_cell(0, 6, res["apply"]["where"], **nl)
    pdf.set_text_color(20, 60, 160)
    pdf.cell(0, 6, res["apply"]["url"], link=res["apply"]["url"], **nl)
    pdf.set_text_color(30, 30, 30)
    if res["status_note"]:
        pdf.set_font("Mukta", "B", 10)
        pdf.multi_cell(0, 5.6, L(T["note"], lang) + ": " + res["status_note"], **nl)
        pdf.set_font("Mukta", "", 10)
    pdf.set_font("Mukta", "", 9)
    pdf.multi_cell(0, 5, res["caveat"], **nl)

    if pdf.get_y() > 297 - 18 - 75:  # keep the covering letter on one page
        pdf.add_page()
    heading(L(T["letter"], lang))
    body = L(T["letter_body"], lang).format(
        to=res["apply"]["where"], scheme=L(s["name"], lang), name=p.get("name") or blank, age=p.get("age") or "____",
        village=p.get("village") or "________", district=p.get("district") or "________")
    pdf.set_font("Mukta", "", 10)
    pdf.multi_cell(0, 5.6, body, **nl)
    pdf.ln(2)
    pdf.multi_cell(0, 7, L(T["sign"], lang), **nl)

    pdf.set_auto_page_break(False)  # footer sits inside the bottom margin
    pdf.set_y(-15)
    pdf.set_font("Mukta", "", 8)
    pdf.set_text_color(110, 110, 110)
    pdf.multi_cell(0, 4, L(T["footer"], lang).format(v=DATA["meta"]["verified_on"], d=date.today().isoformat()), **nl)
    return bytes(pdf.output())


def generate_application_pdf(scheme_id: str, profile: dict, lang: str = "en", out_dir: str = "output") -> str:
    """Write a filled draft application sheet (PDF: details, eligibility, checklist, covering letter) and return its file path."""
    Path(out_dir).mkdir(exist_ok=True)
    path = Path(out_dir) / f"{scheme_id}_{lang}.pdf"
    path.write_bytes(build_pdf(scheme_id, profile, lang))
    return str(path.resolve())


# One registry: the web app calls these, and mcp_server.py exposes the same functions over MCP.
TOOLS = {
    "list_schemes": list_schemes,
    "parse_citizen_text": parse_citizen_text,
    "check_eligibility": check_eligibility,
    "get_document_checklist": get_document_checklist,
    "generate_application_pdf": generate_application_pdf,
}
