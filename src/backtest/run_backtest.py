"""5 BIST hissesi icin 2024-2026 backtest raporu + BIST100 karsilastirmasi."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.backtest.engine import run_backtest


def main():
    tickers = ["THYAO.IS", "GARAN.IS", "ASELS.IS", "KCHOL.IS", "TUPRS.IS"]
    res = run_backtest(tickers, start="2024-01-01", end="2026-06-06",
                       window=10, horizon=5)

    out = Path(__file__).resolve().parents[2] / "data" / "backtest.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    a = res["ayar"]
    print(f"BACKTEST {a['start']} -> {a['end']} | pencere={a['window']} ufuk={a['horizon']} gun\n")
    print(f"{'HISSE':10s} {'SINYAL':>6s} {'YONLU':>5s} {'ISABET':>6s} {'BASARI%':>7s} {'STRAT%':>8s} {'ALTUT%':>8s}")
    print("-" * 64)
    for p in res["hisseler"]:
        br = p["basari_orani_%"]
        print(f"{p['symbol']:10s} {p['sinyal']:>6d} {p['yonlu_sinyal']:>5d} "
              f"{p['isabet']:>6d} {(str(br)+'%') if br is not None else '-':>7s} "
              f"{p['strateji_getiri_%']:>7.1f}% {p['al_tut_getiri_%']:>7.1f}%")

    o = res["ozet"]
    print("\n" + "=" * 64)
    print(f"Agirlikli basari orani : %{o['agirlikli_basari_orani_%']} "
          f"({o['toplam_isabet']}/{o['toplam_yonlu_sinyal']} yonlu sinyal)")
    print(f"Portfoy strateji getiri: %{o['portfoy_strateji_getiri_%']}")
    print(f"Portfoy al-tut getiri  : %{o['portfoy_al_tut_getiri_%']}")
    print(f"BIST100 al-tut getiri  : %{o['bist100_al_tut_getiri_%']}")
    print(f"Strateji - BIST100     : %{o['strateji_vs_bist100_fark_%']}")
    print(f"\nKaydedildi: {out}")
    print("NOT: Getiriler NOMINAL (TRY); islem maliyeti/slipaj yok; long/flat; "
          "puan deterministik skorlayicidan (canli Claude degil).")


if __name__ == "__main__":
    main()
