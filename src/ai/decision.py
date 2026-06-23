"""Puan -> karar esik tablosu.

7-10 = AL          (AL esigi 8'den 7'ye indirildi: bot daha az temkinli)
6    = AL (temkinli)
4-5  = TUT
2-3  = SAT
1    = Guclu SAT
"""


def decision_from_score(score: int) -> tuple[str, str]:
    """(kod, etiket) dondurur."""
    if score >= 7:                     # AL esigi 7 (onceden 8)
        return "AL", "AL"
    if score >= 6:
        return "AL_TEMKINLI", "AL (temkinli)"
    if score >= 4:
        return "TUT", "TUT"
    if score >= 2:
        return "SAT", "SAT"
    return "GUCLU_SAT", "Guclu SAT"
