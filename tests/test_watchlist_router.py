# tests/test_watchlist_router.py
#
# Direct unit tests for app/routers/watchlist.py's FUT.GG source-branching
# (list_watch_items / refresh_watch_item), calling the router functions
# directly with fake wdb/pdb connections and a fake FutggMarketDataProvider
# (via monkeypatching _futgg_provider) instead of spinning up a real DB or
# HTTP stack. pytest-asyncio isn't installed in this repo's test env (no
# other test file here uses it either), so each test is a plain sync
# function that drives its async body with asyncio.run() - same approach
# auto_sync's tests/test_futgg_price_outcomes.py uses for its DB-backed
# cases.
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from app.routers import watchlist as watchlist_router


def run_async(coro_fn):
    return asyncio.run(coro_fn())


class FakeRequest:
    def __init__(self, user_id: str = "u1"):
        self.session = {"user_id": user_id}


class FakeWatchDB:
    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None):
        self.rows = rows or []
        self.executed: List[Any] = []

    async def fetch(self, query: str, *args):
        return [dict(r) for r in self.rows]

    async def fetchrow(self, query: str, *args):
        if "SELECT * FROM watchlist WHERE id" in query:
            watch_id = args[0]
            for r in self.rows:
                if r["id"] == watch_id:
                    return dict(r)
            return None
        if "INSERT INTO watchlist" in query:
            return {"id": 99}
        return None

    async def fetchval(self, query: str, *args):
        return 0

    async def execute(self, query: str, *args):
        self.executed.append((query, args))
        return "UPDATE 1"


class FakePlayerDB:
    async def fetch(self, query: str, *args):
        return []

    async def fetchrow(self, query: str, *args):
        return None


class FakeFutggProvider:
    def __init__(self, players: Optional[Dict[int, Dict[str, Any]]] = None):
        self.players = players or {}
        self.bumped: List[int] = []

    async def get_player(self, card_id: int):
        return self.players.get(card_id)

    async def get_players_by_ids(self, card_ids: List[int]):
        return {cid: self.players[cid] for cid in card_ids if cid in self.players}

    async def bump_price_priority(self, card_id: int) -> None:
        self.bumped.append(card_id)


async def _async_return(value):
    return value


def _futgg_row(**overrides) -> Dict[str, Any]:
    defaults = dict(
        source_card_id=555, name="Erling Haaland", rating=91, club="Man City",
        nation="Norway", rarity="gold_rare", player_image_url="http://img/555.png",
        current_bin=250000, is_tradeable=True,
    )
    defaults.update(overrides)
    return defaults


def test_list_watch_items_enriches_futgg_row_from_snapshot(monkeypatch):
    fake_provider = FakeFutggProvider({555: _futgg_row()})
    monkeypatch.setattr(watchlist_router, "_futgg_provider", lambda: _async_return(fake_provider))

    wdb = FakeWatchDB(rows=[{
        "id": 1, "card_id": 555, "player_name": "Haaland", "version": None,
        "platform": "ps", "started_price": 240000, "started_at": None,
        "last_price": 200000, "last_checked": None, "notes": None,
        "source": "futgg",
    }])
    pdb = FakePlayerDB()

    async def run():
        return await watchlist_router.list_watch_items(FakeRequest(), wdb=wdb, pdb=pdb)

    result = run_async(run)

    assert result["ok"] is True
    item = result["items"][0]
    assert item["source"] == "futgg"
    assert item["current_price"] == 250000
    assert item["name"] == "Erling Haaland"
    assert item["club"] == "Man City"
    assert item["is_extinct"] is False


def test_list_watch_items_futgg_row_missing_from_snapshot_is_extinct(monkeypatch):
    fake_provider = FakeFutggProvider({})
    monkeypatch.setattr(watchlist_router, "_futgg_provider", lambda: _async_return(fake_provider))

    wdb = FakeWatchDB(rows=[{
        "id": 2, "card_id": 999, "player_name": "Ghost Card", "version": None,
        "platform": "ps", "started_price": 1000, "started_at": None,
        "last_price": 1000, "last_checked": None, "notes": None,
        "source": "futgg",
    }])
    pdb = FakePlayerDB()

    async def run():
        return await watchlist_router.list_watch_items(FakeRequest(), wdb=wdb, pdb=pdb)

    result = run_async(run)

    item = result["items"][0]
    assert item["is_extinct"] is True
    assert item["current_price"] is None
    assert item["name"] == "Ghost Card"


def test_list_watch_items_futbin_row_uses_last_price_not_snapshot(monkeypatch):
    fake_provider = FakeFutggProvider({})
    monkeypatch.setattr(watchlist_router, "_futgg_provider", lambda: _async_return(fake_provider))

    wdb = FakeWatchDB(rows=[{
        "id": 3, "card_id": 111, "player_name": "Legacy Card", "version": None,
        "platform": "ps", "started_price": 5000, "started_at": None,
        "last_price": 5500, "last_checked": None, "notes": None,
        "source": "futbin",
    }])
    pdb = FakePlayerDB()

    async def run():
        return await watchlist_router.list_watch_items(FakeRequest(), wdb=wdb, pdb=pdb)

    result = run_async(run)

    item = result["items"][0]
    assert item["source"] == "futbin"
    assert item["current_price"] == 5500
    assert item["is_extinct"] is False


def test_refresh_watch_item_futgg_bumps_priority_and_reads_snapshot(monkeypatch):
    fake_provider = FakeFutggProvider({555: _futgg_row(current_bin=260000)})
    monkeypatch.setattr(watchlist_router, "_futgg_provider", lambda: _async_return(fake_provider))

    wdb = FakeWatchDB(rows=[{
        "id": 1, "card_id": 555, "player_name": "Haaland", "version": None,
        "platform": "ps", "started_price": 240000, "started_at": None,
        "last_price": 200000, "last_checked": None, "notes": None,
        "source": "futgg",
    }])
    pdb = FakePlayerDB()

    async def run():
        return await watchlist_router.refresh_watch_item(1, FakeRequest(), wdb=wdb, pdb=pdb)

    result = run_async(run)

    assert fake_provider.bumped == [555]
    assert result["item"]["current_price"] == 260000
    assert result["item"]["source"] == "futgg"
    assert result["item"]["is_extinct"] is False
    updated = [e for e in wdb.executed if "UPDATE watchlist SET last_price" in e[0]]
    assert updated and updated[0][1] == (260000, 1)


def test_refresh_watch_item_futgg_extinct_when_missing(monkeypatch):
    fake_provider = FakeFutggProvider({})
    monkeypatch.setattr(watchlist_router, "_futgg_provider", lambda: _async_return(fake_provider))

    wdb = FakeWatchDB(rows=[{
        "id": 4, "card_id": 777, "player_name": "Vanished Card", "version": None,
        "platform": "ps", "started_price": 1000, "started_at": None,
        "last_price": 1000, "last_checked": None, "notes": None,
        "source": "futgg",
    }])
    pdb = FakePlayerDB()

    async def run():
        return await watchlist_router.refresh_watch_item(4, FakeRequest(), wdb=wdb, pdb=pdb)

    result = run_async(run)

    assert result["item"]["is_extinct"] is True
    assert result["item"]["current_price"] is None


def test_refresh_watch_item_futbin_path_unaffected(monkeypatch):
    async def fake_fetch_price(card_id, platform):
        return {"price": 6000, "isExtinct": False, "updatedAt": 1234.5}
    monkeypatch.setattr(watchlist_router, "_fetch_price", fake_fetch_price)

    called = {"futgg": False}

    def _should_not_be_called():
        called["futgg"] = True
        raise AssertionError("futbin rows must not touch the FUT.GG provider")
    monkeypatch.setattr(watchlist_router, "_futgg_provider", _should_not_be_called)

    wdb = FakeWatchDB(rows=[{
        "id": 5, "card_id": 222, "player_name": "Old School", "version": None,
        "platform": "ps", "started_price": 5000, "started_at": None,
        "last_price": 5000, "last_checked": None, "notes": None,
        "source": "futbin",
    }])
    pdb = FakePlayerDB()

    async def run():
        return await watchlist_router.refresh_watch_item(5, FakeRequest(), wdb=wdb, pdb=pdb)

    result = run_async(run)

    assert called["futgg"] is False
    assert result["item"]["current_price"] == 6000
    assert result["item"]["source"] == "futbin"
