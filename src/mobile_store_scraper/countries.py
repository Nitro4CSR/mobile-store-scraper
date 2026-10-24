"""Storefront country lists.

``PLAY_COUNTRIES``: every location on Google's "Supported locations for distribution to
Google Play users" table (version 2026/01, published 2026-09-28), 225 codes.

``APP_STORE_COUNTRIES``: every ISO 3166-1 code accepted by the iTunes lookup API, found by
probing all 249 ISO codes on 2026-10-24 (invalid storefronts answer HTTP 400), 184 codes.

Re-validate them any time with ``python bench/bench.py --validate-countries``.
"""
from __future__ import annotations

from .errors import InvalidCountry

PLAY_COUNTRIES: tuple[str, ...] = tuple("""
AD AE AF AG AI AL AM AO AQ AR AT AU AW AZ BA BB BD BE BF BG BH BI BJ BM BN BO BQ BR BS BT BV BW
BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES
ET FI FJ FK FM FR GA GB GD GE GG GH GI GM GN GQ GR GS GT GW GY HK HM HN HR HT HU ID IE IL IM IN IO
IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME
MG MK ML MM MN MO MR MS MT MU MV MW MX MY MZ NA NE NF NG NI NL NO NP NR NU NZ OM PA PE PG PH PK PL
PN PS PT PY QA RO RS RU RW SA SB SC SD SE SG SH SI SK SL SM SN SO SR SS ST SV SZ TC TD TF TG TH TJ
TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VN VU WS XK YE ZA ZM ZW
""".split())

APP_STORE_COUNTRIES: tuple[str, ...] = tuple("""
AD AE AF AG AI AL AM AO AR AT AU AZ BA BB BD BE BF BG BH BJ BM BN BO BR BS BT BW BY BZ CA CD CF CG
CH CI CL CM CN CO CR CV CY CZ DE DK DM DO DZ EC EE EG ES ET FI FJ FM FR GA GB GD GE GH GM GN GR GT
GW GY HK HN HR HU ID IE IL IN IQ IS IT JM JO JP KE KG KH KN KR KW KY KZ LA LB LC LI LK LR LT LU LV
LY MA MC MD ME MG MK ML MM MN MO MR MS MT MU MV MW MX MY MZ NA NE NG NI NL NO NP NR NZ OM PA PE PG
PH PK PL PS PT PW PY QA RO RS RU RW SA SB SC SE SG SI SK SL SN SR ST SV SZ TC TD TH TJ TM TN TO TR
TT TW TZ UA UG US UY UZ VC VE VG VN VU WS XK YE ZA ZM ZW
""".split())

ALL_COUNTRIES: tuple[str, ...] = tuple(sorted(set(PLAY_COUNTRIES) | set(APP_STORE_COUNTRIES)))


def normalize_country(code: str, allowed: tuple[str, ...] | None = None, store: str = "") -> str:
    """Upper-case and validate a two-letter country code (``"uk"`` is accepted for ``GB``)."""
    if not isinstance(code, str) or len(code.strip()) != 2:
        raise InvalidCountry(f"{code!r} is not a two-letter country code")
    c = code.strip().upper()
    if c == "UK":
        c = "GB"
    if allowed is not None and c not in allowed:
        raise InvalidCountry(f"{c} is not a {store or 'store'} storefront")
    return c
