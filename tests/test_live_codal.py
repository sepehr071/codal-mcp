"""Each tool once against the real codal.ir. Keep this small: codal.ir blocks an IP for up to an hour after a burst."""

import pytest

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_live_tools(client):
    r = (await client.call_tool("cd_search_letters", {"symbol": "فولاد", "letter_type": 6})).structured_content
    assert r["total"] > 100 and r["letters"][0]["symbol"] == "فولاد"
    no = r["letters"][0]["tracing_no"]

    r = (await client.call_tool("cd_letter", {"tracing_no": no})).structured_content
    assert r["tracing_no"] == no and (r.get("tables") or r.get("text"))

    r = (await client.call_tool("cd_company", {"query": "فولاد"})).structured_content
    assert r["companies"][0]["name"] == "فولاد مبارکه اصفهان"

    r = (await client.call_tool("cd_letter_types", {"category": 3})).structured_content
    assert any(t["id"] == 58 for t in r["letter_types"])

    r = (await client.call_tool("cd_financial_years", {"symbol": "فولاد"})).structured_content
    assert r["year_ends"] and r["year_ends"][0].endswith("/12/29")
