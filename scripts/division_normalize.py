"""
Consolidates the free-text division names Claude infers from job titles.

Job titles carry the org unit as a suffix, but at wildly inconsistent depth:
"Mobile Core & Transmission, xCDI" and "xCDI" are the same division, as are
"RAUS CoE" and "Robotics, Automation, and Unmanned Systems CoE", and
"CCYC" / "CCYC (Culture, Community and Youth Cluster)". Left raw, HTX shows
72 "divisions" across 174 postings, which makes the division view useless.

Three consolidation passes, in order of confidence:
  1. Formatting - case, &/and, punctuation, whitespace.
  2. Parenthetical acronyms - "Long Name (ABC)" unifies with "ABC" and with
     "ABC (Long Name)".
  3. Anchoring - if a comma-delimited segment of a name is itself a division
     that appears standalone elsewhere, the name folds into that segment
     (the broader unit). Segments are only treated as anchors when they occur
     standalone, so names that merely contain commas ("Robotics, Automation,
     and Unmanned Systems CoE") are never split apart.

Anything unresolved is left exactly as-is: over-merging distinct units would
silently corrupt the division view, which is worse than some residual spread.
"""

import re
from collections import Counter, defaultdict

# Curated equivalences that no string rule can infer. Canonical -> variants.
ALIASES = {
    "RAUS CoE": [
        "Robotics, Automation, and Unmanned Systems CoE",
        "Robotics, Automation & Unmanned Systems CoE",
        "Robotics Automation & Unmanned Systems",
        "Robotics, Automation and Unmanned Systems",
    ],
    "DIF CoE": ["Digital & Information Forensics CoE"],
    "xCyber": ["xCybersecurity"],
    "PSS CoE": ["PSS (PED)"],
}

GENERIC = {"", "unspecified", "n a", "na", "none", "various"}


def _norm(s) -> str:
    s = str(s).strip().lower().replace("&", " and ")
    s = re.sub(r"[^\w\s()]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _strip_parens(s: str):
    """'Long Name (ABC)' -> ('Long Name', 'ABC'). Returns (main, inner|None)."""
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", str(s).strip())
    if not m:
        return str(s).strip(), None
    main, inner = m.group(1).strip(), m.group(2).strip()
    if not main:
        return inner, None
    return main, inner


def _is_acronym_of(acr: str, phrase: str) -> bool:
    """True when `acr` reads as an acronym of `phrase`.

    A bracketed suffix is often the parent or client agency rather than an
    abbreviation - "GeBIZ X (MOF)", "Healthcare Financing (MOH)",
    "Judiciary (IPOS)". Treating those as the same unit would merge a product
    into its client. Only unify when the letters actually track the phrase's
    word initials (as a subsequence, so "and"/"of" may be skipped or counted).
    """
    acr = re.sub(r"[^A-Za-z]", "", acr).upper()
    if not 2 <= len(acr) <= 6:
        return False
    initials = [w[0].upper() for w in re.findall(r"[A-Za-z][\w'&-]*", phrase)]
    if not initials:
        return False
    i = 0
    for ch in initials:
        if i < len(acr) and ch == acr[i]:
            i += 1
    return i == len(acr)


_ALIAS_KEY = {}
for _canon, _variants in ALIASES.items():
    _ALIAS_KEY[_norm(_canon)] = _canon
    for _v in _variants:
        _ALIAS_KEY[_norm(_v)] = _canon


def resolve_divisions(raw_values) -> dict:
    """Map every raw division string -> a consolidated display name.

    `raw_values` may be an iterable of strings or a {value: weight} mapping.
    """
    counts = raw_values if isinstance(raw_values, dict) else Counter(raw_values)
    counts = {str(k).strip(): v for k, v in counts.items() if str(k).strip()}

    # Pass 1+2: base key per raw value, unifying parenthetical acronym forms.
    base_key, key_forms = {}, defaultdict(Counter)
    acro_to_key = {}
    for raw, w in counts.items():
        if _norm(raw) in GENERIC:
            base_key[raw] = "unspecified"
            key_forms["unspecified"]["Unspecified"] += w
            continue
        main, inner = _strip_parens(raw)
        k = _ALIAS_KEY.get(_norm(raw)) or _ALIAS_KEY.get(_norm(main))
        k = _norm(k) if k else _norm(main)
        base_key[raw] = k
        key_forms[k][main if not _ALIAS_KEY.get(_norm(main)) else _ALIAS_KEY[_norm(main)]] += w
        if inner and _is_acronym_of(inner, main):
            # "Long Name (ABC)" teaches us ABC is an alias for this key - but
            # only when ABC really abbreviates the name (see _is_acronym_of).
            acro_to_key.setdefault(_norm(inner), k)

    # Fold bare acronyms into the key their expansion established.
    for raw in list(base_key):
        k = base_key[raw]
        target = acro_to_key.get(k)
        if target and target != k:
            base_key[raw] = target
            for form, w in key_forms.pop(k, Counter()).items():
                key_forms[target][form] += w

    # Pass 3: anchor multi-part names onto a segment that stands alone elsewhere.
    standalone = Counter()
    for raw, w in counts.items():
        k = base_key[raw]
        if "," not in raw:
            standalone[k] += w

    for raw in list(base_key):
        if "," not in raw:
            continue
        segments = [s.strip() for s in raw.split(",") if s.strip()]
        if len(segments) < 2:
            continue
        cands = []
        for seg in segments:
            sk = _ALIAS_KEY.get(_norm(seg))
            sk = _norm(sk) if sk else _norm(seg)
            sk = acro_to_key.get(sk, sk)
            if standalone.get(sk):
                cands.append((standalone[sk], sk))
        if not cands:
            continue
        target = max(cands)[1]
        old = base_key[raw]
        if target == old:
            continue
        base_key[raw] = target
        for form, w in key_forms.pop(old, Counter()).items():
            key_forms[target][form] += w

    # Display name = most common surface form, preferring one without a comma:
    # a group anchored on "PPMC" should be labelled that, not
    # "Security & Mobility Systems, Op Sys (InfoComm), PPMC".
    display = {}
    for raw, k in base_key.items():
        forms = key_forms.get(k)
        if not forms:
            display[raw] = raw
            continue
        simple = Counter({f: w for f, w in forms.items() if "," not in f})
        display[raw] = (simple or forms).most_common(1)[0][0]
    return display
