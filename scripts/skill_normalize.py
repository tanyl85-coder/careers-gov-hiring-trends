"""
Shared skill-name normalization so the same underlying skill isn't tallied under
several spellings ("AWS" vs "Amazon Web Services", "Machine Learning" vs
"Machine learning", "Problem-Solving" vs "Problem Solving").

Two layers:
  1. _norm(): a formatting normalizer (case, separators, punctuation) that merges
     pure spelling/casing variants. This alone collapses the large majority of
     duplicates the LLM produces.
  2. A hand-curated ALIASES whitelist for acronym <-> expansion identity pairs,
     where string normalization can't help. Only clear, verified synonyms are
     listed - acronym auto-matching is deliberately avoided because it produces
     false positives (e.g. "CFD", "SLAM", "TDD" collide with unrelated phrases).

Used by skills_analysis, build_dashboard, weekly_comparison_report and the daily
report summary so every skill tally agrees. Applied at read/aggregate time, so it
fixes already-cached data without re-running the (paid) LLM extraction.
"""

import re
from collections import Counter, defaultdict

# Canonical display name -> variant spellings that mean the same thing.
# Keys and variants are matched case/punctuation-insensitively via _norm().
ALIASES = {
    "AWS": ["Amazon Web Services"],
    "GCP": ["Google Cloud Platform", "Google Cloud"],
    "Azure": ["Microsoft Azure"],
    "Kubernetes": ["K8s"],
    "AI": ["Artificial Intelligence"],
    "Machine Learning": ["ML"],
    "NLP": ["Natural Language Processing"],
    "SRE": ["Site Reliability Engineering"],
    "RAG": ["Retrieval-Augmented Generation", "Retrieval Augmented Generation"],
    "IAM": ["Identity and Access Management", "Identity & Access Management"],
    "GIS": ["Geographic Information System", "Geographic Information Systems"],
    "Infrastructure as Code": ["IaC"],
    "CI/CD": ["Continuous Integration and Continuous Delivery",
              "Continuous Integration/Continuous Delivery",
              "Continuous Integration and Continuous Deployment",
              "Continuous Integration/Continuous Deployment"],
    "JavaScript": ["JS"],
    "TypeScript": ["TS"],
    "PostgreSQL": ["Postgres"],
    "Site Reliability Engineering (SRE)": [],  # placeholder guard, overwritten below
}
# Drop the accidental placeholder if present (keeps the dict literal readable).
ALIASES.pop("Site Reliability Engineering (SRE)", None)


def _norm(s) -> str:
    s = str(s).strip().lower().replace("&", " and ")
    s = re.sub(r"[\-/]", " ", s)          # unify hyphen/slash separators
    s = re.sub(r"[^\w\s+#.]", " ", s)     # drop other punctuation; keep + # . (c++, c#, node.js)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# normalized-variant -> canonical display name
_KEY_CANON = {}
for _canon, _variants in ALIASES.items():
    _KEY_CANON[_norm(_canon)] = _canon
    for _v in _variants:
        _KEY_CANON[_norm(_v)] = _canon


def skill_key(s) -> str:
    """Merge key: identical for all spellings of the same skill."""
    k = _norm(s)
    canon = _KEY_CANON.get(k)
    return _norm(canon) if canon else k


def resolve_displays(raw_counts) -> dict:
    """Given raw skill strings with weights (a Counter, a dict, or an iterable of
    strings), return {skill_key: best display name}. For known aliases the
    curated canonical name wins; otherwise the most frequent raw spelling wins,
    so the label users see is the one that actually dominates the data."""
    if isinstance(raw_counts, dict):
        items = raw_counts.items()
    else:
        items = Counter(raw_counts).items()
    groups = defaultdict(Counter)
    for raw, weight in items:
        groups[skill_key(raw)][str(raw).strip()] += weight
    displays = {}
    for key, variants in groups.items():
        canon = _KEY_CANON.get(key)
        displays[key] = canon or variants.most_common(1)[0][0]
    return displays
