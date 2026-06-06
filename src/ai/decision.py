"""Puan -> karar esik tablosu.

8-10 = AL
6-7  = AL (temkinli)
4-5  = TUT
2-3  = SAT
1    = Guclu SAT
"""


def decision_from_score(score: int) -> tuple[str, str]:
    """(kod, etiket) dondurur."""
    if score >= 8:
        return "AL", "AL"
    if score >= 6:
        return "AL_TEMKINLI", "AL (temkinli)"
    if score >= 4:
        return "TUT", "TUT"
    if score >= 2:
        return "SAT", "SAT"
    return "GUCLU_SAT", "Guclu SAT"
