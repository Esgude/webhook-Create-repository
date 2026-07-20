# -*- coding: utf-8 -*-
"""
TradingView -> Binance Futures webhook koprusu (Omega projesi) — v2
Yenilikler: USDT bazli pozisyon buyuklugu, kaldirac ayari, kapatirken
pozisyonun tamamini otomatik kapatma, sembol adim kuralina yuvarlama.

Beklenen JSON ornekleri:
  ACMA : {"passphrase":"...", "symbol":"SOLUSDT", "action":"open_long",
          "usdt":1000, "kaldirac":5}
         (usdt = POZISYONUN TOPLAM BUYUKLUGU; bloke teminat = usdt/kaldirac)
         Istenirse "usdt" yerine "quantity" ile dogrudan coin adedi verilebilir.
  KAPAMA: {"passphrase":"...", "symbol":"SOLUSDT", "action":"close_long"}
         (miktar verilmezse acik pozisyonun TAMAMI kapatilir)
  action: open_long | close_long | open_short | close_short
"""
import os
import math
import logging
from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
from binance.error import ClientError

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

BINANCE_KEY = os.environ.get("BINANCE_KEY", "")
BINANCE_SECRET = os.environ.get("BINANCE_SECRET", "")
PASSPHRASE = os.environ.get("PASSPHRASE", "")
TESTNET = os.environ.get("TESTNET", "1") == "1"

BASE_URL = "https://demo-fapi.binance.com" if TESTNET else "https://fapi.binance.com"

_PROXY = os.environ.get("FIXIE_URL", "")
_prx = {"http": _PROXY, "https": _PROXY} if _PROXY else None
client = UMFutures(key=BINANCE_KEY, secret=BINANCE_SECRET, base_url=BASE_URL, proxies=_prx)

AKSIYONLAR = {
    "open_long":   ("BUY",  False),
    "close_long":  ("SELL", True),
    "open_short":  ("SELL", False),
    "close_short": ("BUY",  True),
}

_adim_onbellek = {}


def adim_boyutu(sembol):
    """Sembolun LOT_SIZE stepSize degeri (orn. SOLUSDT -> 1)."""
    if sembol in _adim_onbellek:
        return _adim_onbellek[sembol]
    bilgi = client.exchange_info()
    for s in bilgi.get("symbols", []):
        if s.get("symbol") == sembol:
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    adim = float(f.get("stepSize", 1))
                    _adim_onbellek[sembol] = adim
                    return adim
    return 1.0


def yuvarla(miktar, adim):
    """Miktari sembol adimina ASAGI yuvarlar (Binance kurali)."""
    if adim <= 0:
        return miktar
    m = math.floor(miktar / adim) * adim
    return int(m) if float(m).is_integer() else round(m, 8)


@app.route("/", methods=["GET"])
def saglik():
    mod = "TESTNET (sahte para)" if TESTNET else "GERCEK HESAP"
    return f"Webhook koprusu calisiyor — mod: {mod}", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify(hata="JSON okunamadi"), 400

    if not PASSPHRASE or data.get("passphrase") != PASSPHRASE:
        app.logger.warning("Yanlis passphrase ile istek geldi!")
        return jsonify(hata="passphrase yanlis"), 401

    aksiyon = str(data.get("action", "")).lower()
    if aksiyon not in AKSIYONLAR:
        return jsonify(hata=f"gecersiz action: {aksiyon}"), 400

    ham = data.get("symbol") or data.get("ticker") or ""
    sembol = str(ham).upper().replace("BINANCE:", "").replace(".P", "").strip()
    if not sembol:
        return jsonify(hata="symbol alani bos"), 400
    taraf, kapat = AKSIYONLAR[aksiyon]
    uyarilar = []

    try:
        # 1) Kaldirac (verildiyse; sembol bazinda kalicidir)
        kaldirac = data.get("kaldirac") or data.get("leverage")
        if kaldirac and not kapat:
            try:
                client.change_leverage(symbol=sembol, leverage=int(kaldirac))
            except ClientError as e:
                uyarilar.append(f"kaldirac ayarlanamadi: {e.error_message}")

        # 2) Miktari belirle
        if kapat and "quantity" not in data and "usdt" not in data:
            # Kapatma: acik pozisyonun tamamini kapat
            poz = client.get_position_risk(symbol=sembol)
            net = 0.0
            for p in poz:
                if p.get("symbol") == sembol:
                    net += float(p.get("positionAmt", 0))
            miktar = abs(net)
            if miktar == 0:
                return jsonify(durum="ok", not_="acik pozisyon yok, emir gonderilmedi"), 200
            miktar = yuvarla(miktar, adim_boyutu(sembol))
        elif "usdt" in data:
            usdt = float(data["usdt"])
            fiyat = float(client.ticker_price(sembol)["price"])
            adim = adim_boyutu(sembol)
            miktar = yuvarla(usdt / fiyat, adim)
            if miktar <= 0:
                return jsonify(hata=f"usdt cok kucuk: {usdt} USDT, fiyat {fiyat}, "
                                    f"min adim {adim} (min ~{fiyat*adim:.0f} USDT gerekli)"), 400
        else:
            miktar = float(data.get("quantity", 1))
            miktar = yuvarla(miktar, adim_boyutu(sembol))

        # 3) Emri gonder
        emir = dict(symbol=sembol, side=taraf, type="MARKET", quantity=miktar)
        if kapat:
            emir["reduceOnly"] = "true"
        sonuc = client.new_order(**emir)
        app.logger.info("EMIR OK: %s %s %s -> orderId=%s",
                        aksiyon, sembol, miktar, sonuc.get("orderId"))
        return jsonify(durum="ok", orderId=sonuc.get("orderId"), symbol=sembol,
                       action=aksiyon, quantity=miktar,
                       uyarilar=uyarilar or None), 200
    except ClientError as e:
        app.logger.error("BINANCE HATASI: %s %s", e.error_code, e.error_message)
        return jsonify(hata=f"Binance: {e.error_code} {e.error_message}"), 500
    except Exception as e:  # noqa: BLE001
        app.logger.error("BEKLENMEDIK HATA: %s", e)
        return jsonify(hata=str(e)), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
