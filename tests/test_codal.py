import time

import httpx
import pytest
from conftest import FIXTURES, fixture, page

from codal_mcp import http

pytestmark = pytest.mark.anyio

Q = "/api/search/v2/q"
DECISION = "/Reports/Decision.aspx"
ATTACHMENT = "/Reports/Attachment.aspx"


def by_tracing(request: httpx.Request) -> httpx.Response:
    """search v2/q filtered by TracingNo, from recorded answers."""
    no = request.url.params.get("TracingNo")
    return httpx.Response(200, json=fixture(f"search_tracing_{no}.json"))


def serve_letters(api):
    api[Q] = by_tracing

    def decision(request):
        serial = request.url.params["LetterSerial"]
        if serial.startswith("q1Twzv"):
            name = "decision_1618277.html"
        else:
            name = (
                "decision_1612323_sheet0.html" if request.url.params.get("sheetId") == "0" else "decision_1612323.html"
            )
        return page(name)(request)

    api[DECISION] = decision
    api[ATTACHMENT] = page("attachment_1618277.html")


async def test_all_tools_are_read_only(client):
    tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == {
        "cd_search_letters",
        "cd_letter",
        "cd_company",
        "cd_letter_types",
        "cd_financial_years",
    }
    for t in tools:
        assert t.annotations.read_only_hint is True, t.name
        assert t.description and t.title, t.name
        assert t.description.split("\n")[0].endswith("."), t.name


async def test_search_sends_site_filters_and_returns_ascii_rows(client, api):
    api[Q] = fixture("search_foolad.json")
    r = (
        await client.call_tool(
            "cd_search_letters",
            {"symbol": " فولاد ", "category": 1, "from_date": "۱۴۰۵-۷-۱", "to_date": "1405/07/30", "period_months": 6},
        )
    ).structured_content
    p = api.calls[0].url.params
    assert (p["Symbol"], p["Category"], p["FromDate"], p["ToDate"], p["Length"]) == (
        "فولاد",
        "1",
        "1405/07/01",
        "1405/07/30",
        "6",
    )
    assert (p["LetterType"], p["TracingNo"], p["search"], p["PageNumber"]) == ("-1", "-1", "true", "1")
    assert "Subject" not in p
    assert (r["total"], r["pages"], r["page"], len(r["letters"])) == (1475, 74, 1, 20)
    first = r["letters"][0]
    assert first == {
        "tracing_no": 1621524,
        "symbol": "فولاد",
        "company": "فولاد مبارکه اصفهان",
        "title": "گزارش فعالیت ماهانه دوره 1 ماهه منتهی به 1405/07/30",
        "sent": "1405/08/06 16:01:48",
        "code": "ن-30",
        "files": ["html", "pdf", "excel", "attachments"],
    }


async def test_search_rejects_gregorian_date(client, api):
    api[Q] = fixture("search_foolad.json")
    r = await client.call_tool("cd_search_letters", {"from_date": "2026-10-01"})
    assert r.is_error and "Jalali" in r.content[0].text and not api.calls


async def test_search_is_cached(client, api):
    api[Q] = fixture("search_foolad.json")
    for _ in range(2):
        await client.call_tool("cd_search_letters", {"symbol": "فولاد"})
    assert len(api.calls) == 1


async def test_letter_text_skips_hidden_templates(client, api):
    serve_letters(api)
    r = (await client.call_tool("cd_letter", {"tracing_no": 1618277})).structured_content
    assert r["title"] == "توضیحات در خصوص اطلاعات و صورت های مالی منتشر شده"
    assert "شرکت فولاد مبارکه اصفهان - نماد: فولاد" in r["text"]
    assert "1612323در تاریخ 1405/07/30" in r["text"]
    assert "توضيحات در خصوص دلايل تغييرات بيش از 30 درصد سود عملياتي" in r["text"]
    assert "لغو پذیرش اوراق بهادار" not in r["text"]  # one of the page's hidden letter templates
    assert r["pdf"] == "https://www.codal.ir/DownloadFile.aspx?hs=q1TwzvITsscrZmqLPHatbw%3d%3d&ft=1005&let=174"
    assert r["attachments"] == [
        {
            "name": "تغيير بيش از 30 درصد سود عملياتي",
            "added": "1405/07/30 14:40",
            "url": "https://www.codal.ir/Reports/DownloadFile.aspx?id=r3PEhjH9wSRoQQQaQQQj2QQQaQQQaPOOObOOOdfw%3d%3d",
        }
    ]
    assert "tables" not in r and "excel" not in r
    decision = next(c for c in api.calls if c.url.path == DECISION)
    assert decision.url.params["LetterSerial"] == "q1TwzvITsscrZmqLPHatbw=="
    assert decision.headers["sec-fetch-mode"] == "navigate" and "origin" not in decision.headers


def test_attachments_parse_both_grid_renderings():
    from codal_mcp.codal import _attachments

    for name in ("attachment_1618277.html", "attachment_1618277_curl.html"):  # Chrome-like and plain-curl markup
        rows = _attachments((FIXTURES / name).read_text(encoding="utf-8"))
        assert [(r["name"], r["added"]) for r in rows] == [("تغيير بيش از 30 درصد سود عملياتي", "1405/07/30 14:40")]


async def test_letter_statement_tables_and_sheets(client, api):
    serve_letters(api)
    r = (await client.call_tool("cd_letter", {"tracing_no": 1612323})).structured_content
    assert (r["period_months"], r["period_end"], r["year_end"], r["audited"]) == (6, "1405/06/31", "1405/12/29", False)
    assert r["unit_note"] == "کلیه مبالغ درج شده به میلیون ریال می باشد"
    assert r["listed_capital"] == "1,935,000,000"
    assert r["sheet"] == 1 and len(r["sheets"]) == 10
    assert {"sheet": 0, "name": "صورت وضعیت مالی"} in r["sheets"]
    income = r["tables"][0]
    assert income["title"] == "صورت سود و زيان"
    # as the site shows it: 3 periods, change vs. same period last year, change vs. last full year
    assert "درآمدهاي عملياتي | 2509422184 | 1608734238 | 3668860246 | 56 | -32" in income["rows"]
    assert not any(row.startswith("1000") for row in income["rows"])  # an all-invisible helper row
    assert r["excel"].startswith("https://excel.codal.ir/service/Excel/GetAll/")

    r = (await client.call_tool("cd_letter", {"tracing_no": 1612323, "sheet": 0})).structured_content
    assert r["sheet"] == 0 and r["tables"][0]["title"] == "صورت وضعيت مالي"
    assert any(row.startswith("جمع دارايي") for row in r["tables"][0]["rows"])


async def test_letter_unknown_tracing_no(client, api):
    api[Q] = lambda r: httpx.Response(200, json={"Total": 0, "Page": 0, "Letters": []})
    r = await client.call_tool("cd_letter", {"tracing_no": 999})
    assert r.is_error and "No codal letter has tracing number 999" in r.content[0].text


async def test_company_lookup(client, api):
    api["/api/search/v1/companies"] = fixture("companies_trim.json")
    api["/api/search/v1/IndustryGroup"] = fixture("industry_groups.json")
    r = (await client.call_tool("cd_company", {"query": "فولاد"})).structured_content
    assert r["companies"][0] == {
        "symbol": "فولاد",
        "name": "فولاد مبارکه اصفهان",
        "isic": "271018",
        "market": "Tehran Stock Exchange (بورس تهران)",
        "kind": "publisher",
        "industry": "فلزات اساسی",
        "reporting_type": "manufacturing",
    }
    assert r["matched"] > 10 and len(r["companies"]) == 10
    # Arabic kaf and yeh in the query still match the Persian spelling
    r = (await client.call_tool("cd_company", {"query": "فولاد مباركه اصفهان"})).structured_content
    assert r["companies"][0]["symbol"] == "فولاد"
    # an exact company name beats unlisted companies whose symbol starts with it
    r = (await client.call_tool("cd_company", {"query": "ایران خودرو"})).structured_content
    assert r["companies"][0]["symbol"] == "خودرو"
    assert len([c for c in api.calls if c.url.path.endswith("companies")]) == 1  # cached


async def test_letter_types(client, api):
    api["/api/search/v1/categories"] = fixture("categories.json")
    r = (await client.call_tool("cd_letter_types", {})).structured_content
    assert len(r["categories"]) == 11 and r["categories"][2]["category"] == 3
    r = (await client.call_tool("cd_letter_types", {"category": 3})).structured_content
    assert {"id": 58, "name": "گزارش فعالیت ماهانه"} in r["letter_types"]
    assert len({t["id"] for t in r["letter_types"]}) == len(r["letter_types"])


async def test_financial_years(client, api):
    api["/api/search/v1/financialYears"] = fixture("financial_years_foolad.json")
    r = (await client.call_tool("cd_financial_years", {"symbol": "فولاد"})).structured_content
    assert r["year_ends"][:2] == ["1405/12/29", "1404/12/29"]
    assert api.calls[0].url.params["Symbol"] == "فولاد"


async def test_rate_limit_gives_clear_error_and_pauses_calls(client, api):
    api[Q] = lambda r: httpx.Response(429, text="The custom error module does not recognize this error.")
    r = await client.call_tool("cd_search_letters", {"symbol": "فولاد"})
    assert r.is_error and "rate limiting" in r.content[0].text and "wait about 60 seconds" in r.content[0].text
    r = await client.call_tool("cd_search_letters", {"symbol": "خودرو"})
    assert r.is_error and "rate limiting" in r.content[0].text
    assert len(api.calls) == 1  # no retry, and no new request during the pause


async def test_captcha_page_counts_as_rate_limit(client, api):
    api[Q] = by_tracing
    api[DECISION] = page("decision_captcha_wall.html")
    r = await client.call_tool("cd_letter", {"tracing_no": 1618277})
    assert r.is_error and "rate limiting" in r.content[0].text


async def test_requests_are_spaced(client, api, monkeypatch):
    monkeypatch.setattr(http, "MIN_INTERVAL", 0.3)
    api[Q] = fixture("search_foolad.json")
    start = time.monotonic()
    await client.call_tool("cd_search_letters", {"symbol": "فولاد"})
    await client.call_tool("cd_search_letters", {"symbol": "فولاد", "page": 2})
    assert time.monotonic() - start >= 0.25 and len(api.calls) == 2  # Windows timers are ~16 ms coarse


async def test_search_headers_look_like_codal_page(client, api):
    api[Q] = fixture("search_foolad.json")
    await client.call_tool("cd_search_letters", {})
    h = api.calls[0].headers
    assert h["origin"] == "https://www.codal.ir" and h["referer"] == "https://www.codal.ir/"
    assert h["accept"] == "application/json, text/plain, */*" and h["sec-fetch-site"] == "same-site"


async def test_server_error_is_retried_once(client, api):
    calls = []

    def flaky(request):
        calls.append(1)
        return httpx.Response(502) if len(calls) == 1 else httpx.Response(200, json=fixture("search_foolad.json"))

    api[Q] = flaky
    r = await client.call_tool("cd_search_letters", {})
    assert not r.is_error and len(calls) == 2
