from datetime import date, datetime, timezone

from invest_bot import cloud, commands, mnb, scan, state
from test_commands import download, info

SOAP = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><GetExchangeRatesResponse '
        'xmlns="http://www.mnb.hu/webservices/"><GetExchangeRatesResult>&lt;MNBExchangeRates&gt;'
        '&lt;Day date="2026-10-26"&gt;&lt;Rate unit="1" curr="EUR"&gt;366,95000&lt;/Rate&gt;'
        '&lt;Rate unit="100" curr="JPY"&gt;208,10000&lt;/Rate&gt;&lt;Rate unit="1" curr="USD"&gt;327,49000&lt;/Rate&gt;'
        '&lt;/Day&gt;&lt;Day date="2026-10-22"&gt;&lt;Rate unit="1" curr="USD"&gt;326,23000&lt;/Rate&gt;'
        '&lt;Rate unit="1" curr="GBP"&gt;436,00000&lt;/Rate&gt;&lt;/Day&gt;&lt;/MNBExchangeRates&gt;'
        '</GetExchangeRatesResult></GetExchangeRatesResponse></s:Body></s:Envelope>').encode()


class Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def test_parse_handles_comma_decimals_and_units():
    days = mnb.parse(SOAP)
    assert days["2026-10-26"] == {"EUR": 366.95, "JPY": 2.081, "USD": 327.49}  # JPY is quoted per 100
    assert days["2026-10-22"]["USD"] == 326.23


def test_fetch_posts_the_soap_request():
    seen = []

    def opener(req, timeout):
        seen.append(req)
        return Resp(SOAP)
    assert mnb.fetch(["USD", "EUR"], date(2026, 10, 16), date(2026, 10, 26), opener)["2026-10-26"]["USD"] == 327.49
    body = seen[0].data.decode()
    assert seen[0].full_url == mnb.URL and seen[0].get_header("Soapaction") == mnb.ACTION
    assert ("<web:startDate>2026-10-16</web:startDate><web:endDate>2026-10-26</web:endDate>"
            "<web:currencyNames>USD,EUR</web:currencyNames>") in body


def test_rate_uses_the_last_published_day_and_caches_final_rates():
    calls = []

    def fetch(ccys, start, end):
        calls.append((ccys, start, end))
        return mnb.parse(SOAP)
    cache = {}
    assert mnb.rate("USD", date(2026, 10, 26), cache, date(2026, 10, 27), fetch) == 327.49
    assert mnb.rate("USD", date(2026, 10, 26), cache, date(2026, 10, 27), fetch) == 327.49 and len(calls) == 1
    assert calls[0] == (["USD"], date(2026, 10, 16), date(2026, 10, 26))
    # 23 Oct (a holiday) and the weekend use 22 Oct's rate
    assert mnb.rate("USD", date(2026, 10, 25), cache, date(2026, 10, 27), fetch) == 326.23
    assert mnb.rate("GBp", date(2026, 10, 24), cache, date(2026, 10, 27), fetch) == 4.36  # pence
    assert mnb.rate("HUF", date(2026, 10, 24), cache) == 1.0
    assert cache == {"USD|2026-10-26": 327.49, "USD|2026-10-25": 326.23, "GBP|2026-10-24": 436.0}
    # today before MNB published today's rate: not final, so not used or cached
    assert mnb.rate("USD", date(2026, 10, 27), cache, date(2026, 10, 27), fetch) is None
    assert "USD|2026-10-27" not in cache
    assert mnb.rate("CHF", date(2026, 10, 26), {}, date(2026, 10, 27), fetch) is None

    def down(*a):
        raise OSError("no network")
    assert mnb.rate("EUR", date(2026, 10, 26), {}, date(2026, 10, 27), down) is None


def test_trades_record_the_mnb_rate_and_tax_fills_missing_ones():
    def fetch(ccys, start, end):
        return mnb.parse(SOAP)

    def down(*a):
        raise OSError("no network")
    mon = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)
    st = state.empty()
    r = commands.handle(st, "/bought NVDA 2 100", mon, download, info, fetch)
    assert st["log"][-1]["fx"] == 350.0 and st["log"][-1]["fx_mnb"] == 327.49 and "MNB rate 327.49 HUF/USD" in r
    assert st["mnb"] == {"USD|2026-10-26": 327.49}
    tue = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    r = commands.handle(st, "/bought NVDA 1 100", tue, download, info, down)
    assert st["log"][-1]["fx_mnb"] is None and "No MNB rate yet: tax uses Yahoo FX (approx)" in r
    commands.handle(st, "/bought MOL.BD 1 3000", mon, download, info, down)
    assert "fx_mnb" not in st["log"][-1]  # HUF needs none
    r = commands.handle(st, "/tax", datetime(2026, 10, 28, 9, tzinfo=timezone.utc), download, info, fetch)
    assert st["log"][1]["fx_mnb"] == 327.49 and "at the official MNB rates" in r


def test_distributing_etf_says_it_pays_taxable_dividends():
    assert [s for s, v in scan.UNIVERSE.items() if v[1] == "etf" and v[3] == "dist"] == ["VHYL.AS"]
    assert all(v[3] == "acc" for s, v in scan.UNIVERSE.items() if v[1] == "etf" and s != "VHYL.AS")
    from test_scan import FX, NOW, hist
    items = scan.run_scan({"VHYL.AS": hist(), "CSPX.L": hist()}, {}, NOW, 1e6, {}, FX)
    by = {i["symbol"]: i for i in items}
    assert by["VHYL.AS"]["dist"] and not by["CSPX.L"]["dist"]
    assert "   💸 pays dividends (taxable)" in cloud.buy_lines(by["VHYL.AS"], NOW)
    assert cloud.DIST not in "\n".join(cloud.buy_lines(by["CSPX.L"], NOW))
