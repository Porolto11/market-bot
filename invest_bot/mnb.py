"""Official MNB exchange rates for the tax figures (Hungarian tax converts a gain at the MNB rate of the buy day and of
the sell day). The free MNB SOAP service, no key, stdlib only:

    http://www.mnb.hu/arfolyamok.asmx  GetExchangeRates(startDate, endDate, currencyNames)

The answer is an XML string inside the SOAP envelope: <Day date="..."><Rate unit="100" curr="JPY">208,1</Rate>...,
comma decimals, a rate per `unit` units. MNB publishes on Hungarian business days (around noon); a day without its
own rate uses the last one published before it.
"""
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from xml.sax.saxutils import escape

URL = "http://www.mnb.hu/arfolyamok.asmx"
ACTION = '"http://www.mnb.hu/webservices/MNBArfolyamServiceSoap/GetExchangeRates"'
BODY = ('<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:web="http://www.mnb.hu/webservices/"><soap:Body><web:GetExchangeRates><web:startDate>{start}'
        '</web:startDate><web:endDate>{end}</web:endDate><web:currencyNames>{ccys}</web:currencyNames>'
        '</web:GetExchangeRates></soap:Body></soap:Envelope>')
LOOKBACK = 10  # days searched back for the last published rate (weekends, holidays)
PENCE = {"GBp": "GBP", "GBX": "GBP"}  # LSE pence quotes: GBP / 100


def parse(xml) -> dict:
    """{ISO date: {currency: HUF per 1 unit}} from a GetExchangeRates SOAP response."""
    result = next(e.text for e in ET.fromstring(xml).iter() if e.tag.endswith("GetExchangeRatesResult"))
    out = {}
    for day in ET.fromstring(result or "<MNBExchangeRates/>").iter("Day"):
        out[day.get("date")] = {r.get("curr"): float(r.text.replace(",", ".")) / int(r.get("unit") or 1)
                                for r in day.iter("Rate") if r.text}
    return out


def fetch(ccys, start, end, opener=urllib.request.urlopen) -> dict:
    """parse() of the MNB rates of `ccys` from start to end (dates or ISO strings). Raises when MNB is unreachable."""
    body = BODY.format(start=start, end=end, ccys=escape(",".join(ccys))).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "text/xml; charset=utf-8",
                                                          "SOAPAction": ACTION})
    with opener(req, timeout=15) as r:
        return parse(r.read())


def rate(ccy, day, cache, today=None, fetch_fn=fetch):
    """HUF per 1 unit of ccy at MNB: the last rate published on or before `day` (a date). HUF: 1.0. None when MNB
    cannot be reached or has no such rate, or when `day` is today and its own rate is not out yet. A final rate (a
    past day, or the day's own rate) is kept in `cache` ({'USD|2026-10-26': 327.49}), so each day is fetched once."""
    if ccy == "HUF":
        return 1.0
    base = PENCE.get(ccy, ccy)
    key = f"{base}|{day.isoformat()}"
    if key not in cache:
        try:
            days = fetch_fn([base], day - timedelta(days=LOOKBACK), day)
        except Exception:
            return None
        found = sorted(d for d, r in days.items() if d <= day.isoformat() and base in r)
        if not found:
            return None
        if found[-1] != day.isoformat() and day >= (today or date.today()):
            return None  # today's rate is published around noon: try again later
        cache[key] = days[found[-1]][base]
    return cache[key] / (100 if ccy in PENCE else 1)
