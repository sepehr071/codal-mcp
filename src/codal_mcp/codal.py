"""Codal tools: search disclosures, read one letter, look up companies, letter types and fiscal years."""

from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field

from .http import SEARCH, WWW, ApiError, fetch
from .registry import tool

LOOKUP_TTL = 12 * 3600  # companies, industry groups, categories: change rarely
LETTER_TTL = 3600  # a published letter does not change; keep it an hour
MAX_TEXT = 16_000  # characters of letter text or tables returned at once

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "0123456789" * 2)
_LETTERS = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "\u200c": " ", "\u200f": None, "\u200e": None})

# v1/companies field `st`, as the search page's PublisherStatus list names them
MARKETS = {
    0: "Tehran Stock Exchange (بورس تهران)",
    1: "Iran Fara Bourse (فرابورس)",
    2: "registered, not listed (ثبت شده پذیرفته نشده)",
    3: "not registered with SEO (ثبت نشده نزد سازمان)",
    4: "Iran Mercantile Exchange (بورس کالا)",
    5: "Iran Energy Exchange (بورس انرژی)",
}
# v1/companies field `t`, the publisher types of v1/categories
KINDS = {1: "publisher", 2: "broker", 3: "financial institution or fund", 4: "public entity", 5: "state company"}
# v1/companies field `RT`, the search page's ReportingType list
REPORTING = {
    1000000: "manufacturing",
    1000001: "construction",
    1000002: "investment",
    1000003: "bank",
    1000004: "leasing",
    1000005: "services",
    1000006: "insurance",
    1000007: "sea transport",
    1000008: "agriculture",
    1000009: "investment bank (تامین سرمایه)",
}


def _ascii(v: Any) -> str:
    """Persian/Arabic digits to ASCII and runs of spaces collapsed, as the site renders them."""
    return re.sub(r"\s+", " ", str(v or "").translate(_DIGITS)).strip()


def _key(v: str) -> str:
    """Matching form of a symbol or name: ASCII digits, Persian ی/ک, no ZWNJ, single spaces."""
    return re.sub(r"\s+", " ", v.translate(_DIGITS).translate(_LETTERS)).strip().lower()


def _jalali(v: str | None, name: str) -> str | None:
    if not v:
        return None
    s = _ascii(v).replace("-", "/")
    m = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if not m or not 1300 <= int(m[1]) <= 1500 or not 1 <= int(m[2]) <= 12 or not 1 <= int(m[3]) <= 31:
        raise ApiError(f"{name} must be a Jalali (Persian calendar) date like 1405/07/01, got {v!r}.")
    return f"{m[1]}/{int(m[2]):02d}/{int(m[3]):02d}"


def _symbol(v: str | None) -> str | None:
    return _ascii(v) or None


async def _search(params: dict[str, Any], ttl: float) -> dict[str, Any]:
    # The flags codal's own search page sends (setDefaultSearchModel); -1 means any.
    query = {
        "Audited": "true",
        "AuditorRef": -1,
        "Category": -1,
        "Childs": "true",
        "CompanyState": -1,
        "CompanyType": -1,
        "Consolidatable": "true",
        "IsNotAudited": "false",
        "Length": -1,
        "LetterType": -1,
        "Mains": "true",
        "NotAudited": "true",
        "NotConsolidatable": "true",
        "PageNumber": 1,
        "Publisher": "false",
        "ReportingType": -1,
        "TracingNo": -1,
        "search": "true",
        **{k: v for k, v in params.items() if v is not None},
    }
    body = await fetch(f"{SEARCH}/v2/q", query, ttl=ttl)
    if not isinstance(body, dict) or not isinstance(body.get("Letters"), list):
        raise ApiError("codal.ir returned no search result.")
    return body


def _row(x: dict[str, Any]) -> dict[str, Any]:
    files = [
        name
        for name, flag in (
            ("html", "HasHtml"),
            ("pdf", "HasPdf"),
            ("excel", "HasExcel"),
            ("attachments", "HasAttachment"),
        )
        if x.get(flag)
    ]
    row = {
        "tracing_no": x.get("TracingNo"),
        "symbol": _ascii(x.get("Symbol")),
        "company": _ascii(x.get("CompanyName")),
        "title": _ascii(x.get("Title")),
        "sent": _ascii(x.get("SentDateTime")),
        "code": _ascii(x.get("LetterCode")),
        "files": files,
    }
    if x.get("PublishDateTime") and x.get("PublishDateTime") != x.get("SentDateTime"):
        row["published"] = _ascii(x["PublishDateTime"])
    if x.get("UnderSupervision"):
        row["under_supervision"] = True
    return row


@tool("Search Codal disclosures")
async def cd_search_letters(
    symbol: Annotated[
        str | None,
        Field(description="Exact Persian trading symbol, e.g. فولاد (find with cd_company)."),
    ] = None,
    category: Annotated[
        int | None,
        Field(
            description="Letter group: 1 financial statements, dividend schedules; 2 material info, clarifications; 3 monthly activity, portfolios; 4 articles, prospectuses; 5 board, audit committee; 6 shareholder meetings; 7 capital increase; 8 exchange notices; 9 SEO notices; 10 other; 11 debt securities.",
            ge=1,
            le=11,
        ),
    ] = None,
    letter_type: Annotated[
        int | None,
        Field(
            description="Letter type id from cd_letter_types, e.g. 6 financial statements, 58 monthly activity, 20 AGM decisions, 30 dividend schedule.",
            ge=1,
        ),
    ] = None,
    title: Annotated[
        str | None,
        Field(description="Persian words in the letter title, e.g. افزایش سرمایه."),
    ] = None,
    from_date: Annotated[str | None, Field(description="Sent on/after this Jalali date, e.g. 1405/07/01.")] = None,
    to_date: Annotated[str | None, Field(description="Sent on/before this Jalali date.")] = None,
    period_months: Annotated[
        int | None,
        Field(
            description="Report period in months: 12 annual, 6 half-year, 3 quarterly, 1 monthly.",
            ge=1,
            le=12,
        ),
    ] = None,
    tracing_no: Annotated[int | None, Field(description="Tracing number (شماره پیگیری) of one letter.", ge=1)] = None,
    page: Annotated[int, Field(description="Result page (20 letters).", ge=1)] = 1,
) -> dict[str, Any]:
    """Search codal.ir disclosures (letters) of Iranian listed companies, newest first, 20 per page.

    No filters lists the newest letters of all companies. `total` = matches, `pages` = page count. Pass a
    letter's `tracing_no` to cd_letter to read it. No results: check the symbol with cd_company or widen filters.
    """
    body = await _search(
        {
            "Symbol": _symbol(symbol),
            "Category": category,
            "LetterType": letter_type,
            "Subject": _ascii(title) or None,
            "FromDate": _jalali(from_date, "from_date"),
            "ToDate": _jalali(to_date, "to_date"),
            "Length": period_months,
            "TracingNo": tracing_no,
            "PageNumber": page,
        },
        ttl=120,
    )
    return {
        "total": body.get("Total"),
        "page": page,
        "pages": body.get("Page"),
        "letters": [_row(x) for x in body["Letters"] if isinstance(x, dict)],
    }


class _Text(HTMLParser):
    """Visible text of a letter page: drops scripts, styles and anything inside a display:none element.

    Table cells are joined with ' | ', one table row per line.
    """

    VOID = {"br", "img", "input", "meta", "link", "hr", "col", "area", "base", "wbr", "source"}
    BLOCK = {"div", "p", "tr", "table", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "ul", "ol", "form"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[tuple[str, bool]] = []  # (tag, hidden)

    def _hidden(self) -> bool:
        return bool(self.stack) and self.stack[-1][1]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        style = re.sub(r"\s", "", (a.get("style") or "").lower())
        hidden = (
            self._hidden()
            or tag in ("script", "style", "head", "select", "option", "noscript")
            or "display:none" in style
            or a.get("type") == "hidden"
        )
        if tag in self.VOID:
            if tag == "br" and not hidden:
                self.out.append("\n")
            return
        self.stack.append((tag, hidden))
        if not hidden and tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        # Pop up to the matching tag; letter pages are not always well nested.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                hidden = self.stack[i][1]
                del self.stack[i:]
                if not hidden:
                    self.out.append(" | " if tag in ("td", "th") else "\n" if tag in self.BLOCK else "")
                return

    def handle_data(self, data):
        if not self._hidden():
            self.out.append(re.sub(r"\s+", " ", data))  # source line breaks are not visible breaks

    def text(self) -> str:
        lines = []
        for line in "".join(self.out).split("\n"):
            line = _ascii(line).strip(" |")
            line = re.sub(r"(\s*\|\s*){2,}", " | ", line)
            if line and line != "|":
                lines.append(line)
        return "\n".join(lines)


def _visible_text(html: str) -> str:
    p = _Text()
    body = html.find("<body")
    p.feed(html[body:] if body >= 0 else html)
    p.close()
    return p.text()


def _datasource(html: str) -> dict[str, Any] | None:
    i = html.find("var datasource = ")
    if i < 0:
        return None
    try:
        d, _ = json.JSONDecoder().raw_decode(html, i + len("var datasource = "))
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _tables(ds: dict[str, Any]) -> list[dict[str, Any]]:
    """Each table of the shown sheet as {title, rows}; a row is its cell values joined by ' | '.

    Rows whose cells are all flagged invisible are hidden helper rows and are dropped. A single invisible cell is
    kept: the site still shows columns such as the second 'درصد تغيير' (change vs. the last full year) that the
    datasource flags isVisible=false (checked in Chrome on tracing no. 1612323).
    """
    out = []
    for sheet in ds.get("sheets") or []:
        for t in sheet.get("tables") or []:
            rows: dict[int, list[dict[str, Any]]] = {}
            for c in t.get("cells") or []:
                if isinstance(c, dict):
                    rows.setdefault(c.get("rowSequence") or 0, []).append(c)
            lines = []
            for k in sorted(rows):
                if not any(c.get("isVisible", True) for c in rows[k]):
                    continue
                vals = [_ascii(c.get("value")) for c in sorted(rows[k], key=lambda c: c.get("columnSequence") or 0)]
                while vals and not vals[-1]:
                    vals.pop()
                if vals:
                    lines.append(" | ".join(vals))
            if lines:
                out.append({"title": _ascii(t.get("title_Fa") or sheet.get("title_Fa")), "rows": lines})
    return out


def _sheets(html: str) -> list[dict[str, Any]]:
    m = re.search(r'<select[^>]*name="ctl00\$ddlTable".*?</select>', html, re.S)
    if not m:
        return []
    return [
        {"sheet": int(v), "name": _ascii(unescape(n))}
        for v, n in re.findall(r'<option[^>]*value="(-?\d+)"[^>]*>([^<]*)', m[0])
    ]


def _label(html: str, id_: str) -> str:
    m = re.search(rf'id="{id_}"[^>]*>([^<]*)<', html)
    return _ascii(unescape(m[1])) if m else ""


def _clip(text: str) -> tuple[str, bool]:
    return (text, False) if len(text) <= MAX_TEXT else (text[:MAX_TEXT], True)


def _attachments(html: str) -> list[dict[str, str]]:
    out = []
    # ASP.NET renders the grid with inline styles for Chrome and without for plain curl; match both.
    rows = re.findall(r"window\.open\('(DownloadFile\.aspx\?id=[^']+)'\);\"[^>]*>(.*?)</tr>", unescape(html), re.S)
    for link, cells in rows:
        tds = [_ascii(re.sub(r"<[^>]+>", " ", td)) for td in re.findall(r"<td[^>]*>(.*?)</td>", cells, re.S)]
        tds = [t for t in tds if t]
        out.append(
            {
                "name": tds[0] if tds else "",
                "added": tds[1] if len(tds) > 1 else "",
                "url": f"{WWW}/Reports/{link}",
            }
        )
    return out


def _split(path: str) -> tuple[str, dict[str, str]]:
    """A site-relative link from the search API -> (absolute URL without query, query params)."""
    u = urlsplit(path)
    p = u.path if u.path.startswith("/") else f"/{u.path}"
    return f"{WWW}{p}", dict(parse_qsl(u.query))


def _link(path: str) -> str:
    if not path:
        return ""
    return path if path.startswith("http") else f"{WWW}{path if path.startswith('/') else '/' + path}"


@tool("Read one Codal letter")
async def cd_letter(
    tracing_no: Annotated[int, Field(description="Tracing number (شماره پیگیری) from cd_search_letters.", ge=1)],
    sheet: Annotated[
        int | None,
        Field(description="Sheet id from the `sheets` list of a previous call; omit for the first sheet."),
    ] = None,
) -> dict[str, Any]:
    """Read one codal.ir letter: its text or tables, plus links to its page, PDF, Excel and attachments.

    Structured reports come back as `tables` (one line per row, cells joined by ' | ', usually million Rial; see
    `unit_note`), one sheet at a time: `sheets` lists them, call again with `sheet` for another. Other letters
    give `text`. `attachments` are the company's files. Cut at 16,000 characters (`truncated`); the PDF has all.
    """
    body = await _search({"TracingNo": tracing_no}, ttl=LETTER_TTL)
    found = [x for x in body["Letters"] if isinstance(x, dict) and x.get("TracingNo") == tracing_no]
    if not found:
        raise ApiError(f"No codal letter has tracing number {tracing_no}. Check the number with cd_search_letters.")
    x = found[0]
    out: dict[str, Any] = _row(x)
    url = x.get("Url") or ""
    out["url"] = _link(url)
    if x.get("HasPdf") and x.get("PdfUrl"):
        out["pdf"] = _link(x["PdfUrl"])  # relative to the site root (checked: /DownloadFile.aspx?hs=... is the PDF)
    if x.get("HasExcel") and x.get("ExcelUrl"):
        out["excel"] = _link(x["ExcelUrl"])

    if x.get("HasHtml") and "Decision.aspx" in url:
        page_url, params = _split(url)
        if sheet is not None:
            params["sheetId"] = str(sheet)
        html = await fetch(page_url, params, text=True, ttl=LETTER_TTL)
        ds = _datasource(html)
        if ds:
            out["report"] = _ascii(ds.get("title_Fa"))
            out["period_months"] = ds.get("period")
            out["period_end"] = _ascii(ds.get("periodEndToDate"))
            out["year_end"] = _ascii(ds.get("yearEndToDate"))
            out["audited"] = ds.get("isAudited")
            out["consolidated"] = ds.get("isConsolidated")
            out["unit_note"] = _label(html, "ctl00_pPriceNote")
            out["listed_capital"] = _label(html, "ctl00_lblListedCapital")
            sheets = _sheets(html)
            if sheets:
                out["sheets"] = sheets
                shown = re.search(r'<option selected="selected" value="(-?\d+)"', html)
                out["sheet"] = int(shown[1]) if shown else sheet
            tables, size = [], 0
            for t in _tables(ds):
                size += sum(len(r) + 1 for r in t["rows"])
                if size > MAX_TEXT and tables:
                    out["truncated"] = True
                    break
                tables.append(t)
            out["tables"] = tables
        else:
            out["text"], truncated = _clip(_visible_text(html))
            if truncated:
                out["truncated"] = True
    if x.get("HasAttachment") and x.get("AttachmentUrl"):
        page_url, params = _split(x["AttachmentUrl"])
        out["attachments"] = _attachments(await fetch(page_url, params, text=True, ttl=LETTER_TTL))
    return out


async def _companies() -> list[dict[str, Any]]:
    body = await fetch(f"{SEARCH}/v1/companies", ttl=LOOKUP_TTL)
    if not isinstance(body, list) or not body:
        raise ApiError("codal.ir returned no company list.")
    return [c for c in body if isinstance(c, dict)]


@tool("Find a company on Codal")
async def cd_company(
    query: Annotated[
        str,
        Field(description="Persian symbol or part of the name, e.g. فولاد."),
    ],
    limit: Annotated[int, Field(description="Max companies.", ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Find companies and funds on codal.ir by symbol or name, with their market, industry and type.

    Use it to get the exact `symbol` for cd_search_letters and cd_financial_years. Exact matches first, then
    prefix, then name contains; listed before unlisted. `matched` is the total number of hits.
    """
    q = _key(query)
    if not q:
        raise ApiError("query must not be empty.")
    companies = await _companies()
    groups = await fetch(f"{SEARCH}/v1/IndustryGroup", ttl=LOOKUP_TTL)
    industry = {g.get("Id"): g.get("Name") for g in groups if isinstance(g, dict)} if isinstance(groups, list) else {}
    ranked = []
    for c in companies:
        sy, name = _key(str(c.get("sy") or "")), _key(str(c.get("n") or ""))
        rank = 0 if q in (sy, name) else 1 if sy.startswith(q) else 2 if q in name else 3 if q in sy else None
        if rank is not None:
            ranked.append((rank, c.get("st") not in (0, 1, 4, 5), len(sy), c))  # exchange-listed first
    ranked.sort(key=lambda r: r[:3])
    rows = []
    for *_, c in ranked[:limit]:
        row = {
            "symbol": _ascii(c.get("sy")),
            "name": _ascii(c.get("n")),
            "isic": _ascii(c.get("i")),
            "market": MARKETS.get(c.get("st")),
            "kind": KINDS.get(c.get("t")),
            "industry": _ascii(industry.get(c.get("IG"))) or None,
            "reporting_type": REPORTING.get(c.get("RT")),
        }
        rows.append(row)
    return {"matched": len(ranked), "companies": rows}


@tool("List Codal letter types")
async def cd_letter_types(
    category: Annotated[
        int | None,
        Field(description="Group code to list its letter types; omit to list the groups.", ge=1, le=11),
    ] = None,
) -> dict[str, Any]:
    """List codal.ir letter groups, or the letter types of one group with the ids cd_search_letters takes.

    No argument: the 11 groups. With `category`: its types; use a type `id` as `letter_type` in cd_search_letters.
    """
    body = await fetch(f"{SEARCH}/v1/categories", ttl=LOOKUP_TTL)
    if not isinstance(body, list):
        raise ApiError("codal.ir returned no category list.")
    cats = [c for c in body if isinstance(c, dict) and isinstance(c.get("Code"), int) and c["Code"] > 0]

    def types(c: dict[str, Any]) -> dict[int, str]:
        seen: dict[int, str] = {}
        for p in c.get("PublisherTypes") or []:
            for lt in p.get("LetterTypes") or []:
                if isinstance(lt, dict) and lt.get("Id") not in seen:
                    seen[lt.get("Id")] = _ascii(lt.get("Name"))
        return seen

    if category is None:
        return {
            "categories": [
                {"category": c["Code"], "name": _ascii(c.get("Name")), "letter_types": len(types(c))} for c in cats
            ]
        }
    match = [c for c in cats if c["Code"] == category]
    if not match:
        raise ApiError(f"Unknown category {category}; call cd_letter_types without arguments for the list.")
    return {
        "category": category,
        "name": _ascii(match[0].get("Name")),
        "letter_types": [{"id": k, "name": v} for k, v in sorted(types(match[0]).items())],
    }


@tool("Fiscal years of a company")
async def cd_financial_years(
    symbol: Annotated[str, Field(description="Exact Persian trading symbol, e.g. فولاد.")],
) -> dict[str, Any]:
    """List the fiscal year-end dates codal.ir knows for one company, newest first (Jalali).

    Use it before searching annual statements with cd_search_letters (period_months 12). An empty list usually
    means a wrong symbol: check cd_company.
    """
    sy = _symbol(symbol)
    if not sy:
        raise ApiError("symbol must not be empty.")
    body = await fetch(f"{SEARCH}/v1/financialYears", {"Symbol": sy}, ttl=LOOKUP_TTL)
    if not isinstance(body, list):
        raise ApiError("codal.ir returned no fiscal years.")
    return {"symbol": sy, "year_ends": [_ascii(y) for y in body]}
