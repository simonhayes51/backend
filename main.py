import os
import json
import requests
import logging
import re
import io
from datetime import datetime
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from bs4 import BeautifulSoup
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as ticker

log = logging.getLogger("dashboard-pricecheck")
log.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter("[%(asctime)s] %(levelname)s:%(name)s: %(message)s")
handler.setFormatter(formatter)
log.addHandler(handler)

app = FastAPI()

# Load players_temp.json once
def load_players():
    try:
        with open("players_temp.json", "r", encoding="utf-8") as f:
            players = json.load(f)
            log.info("[LOAD] players_temp.json loaded successfully.")
            return players
    except Exception as e:
        log.error(f"[ERROR] Failed to load players: {e}")
        return []

PLAYERS = load_players()

# --- Helper: fetch hourly price graph data from FUTBIN ---
def fetch_price_data(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers)
        soup = BeautifulSoup(res.text, "html.parser")

        graph_divs = soup.find_all("div", class_="highcharts-graph-wrapper")
        if len(graph_divs) < 2:
            log.warning("[SCRAPE] Hourly graph not found.")
            return []

        hourly_graph = graph_divs[1]
        data_ps_raw = hourly_graph.get("data-ps-data", "[]")
        price_data = json.loads(data_ps_raw)
        filtered = [(datetime.fromtimestamp(ts / 1000), price)
                    for ts, price in price_data if price > 0]
        return filtered[-24:]
    except Exception as e:
        log.error(f"[ERROR] Failed to fetch hourly data: {e}")
        return []

# --- Helper: generate price graph (lime green, same style as bot) ---
def generate_price_graph(price_data, player_name):
    try:
        if len(price_data) < 2:
            return None
        timestamps, prices = zip(*price_data)
        fig, ax = plt.subplots(figsize=(6, 3))
        ax.plot(
            timestamps, prices,
            marker="o", linestyle="-", color="#39FF14",
            markersize=3, linewidth=2
        )
        ax.set_title(f"{player_name} Price Trend (Today)", fontsize=11, fontweight="bold")
        ax.set_xlabel("Time", fontsize=9)
        ax.set_ylabel("Coins", fontsize=9)
        ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.3)
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        plt.xticks(rotation=45, fontsize=8)
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{int(x/1000)}K"))
        plt.yticks(fontsize=8)
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=220)
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        log.error(f"[ERROR] Failed to generate graph: {e}")
        return None

# --- Main PriceCheck API ---
@app.get("/api/pricecheck")
def pricecheck(player_name: str = Query(...), platform: str = Query("console")):
    """Fetch FUTBIN price data from players_temp.json + scrape graph/trend."""
    match = next((p for p in PLAYERS if f"{p['name']} {p['rating']}".lower() == player_name.lower()), None)
    if not match:
        raise HTTPException(status_code=404, detail="Player not found")

    url = match["url"]
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(res.text, "html.parser")

        price_box = soup.find("div", class_="price-box-original-player")
        price_tag = price_box.find("div", class_="price inline-with-icon lowest-price-1")
        price = price_tag.text.strip().replace(",", "") if price_tag else "N/A"
        price = f"{int(price):,}" if price.isdigit() else price

        trend_tag = price_box.find("div", class_="price-box-trend")
        raw_trend = trend_tag.get_text(strip=True).replace("Trend:", "") if trend_tag else "-"
        clean_trend = re.sub(r"[📉📈]", "", raw_trend).strip()
        trend_emoji = "📉" if "-" in clean_trend else "📈"
        trend = f"{trend_emoji} {clean_trend}"

        range_tag = price_box.find("div", class_="price-pr")
        price_range = range_tag.text.strip().replace("PR:", "") if range_tag else "-"

        updated_tag = price_box.find("div", class_="prices-updated")
        updated = updated_tag.text.strip().replace("Price Updated:", "") if updated_tag else "-"

        # Return JSON response
        return {
            "id": match["id"],
            "name": match["name"],
            "rating": match["rating"],
            "club": match.get("club", "Unknown"),
            "nation": match.get("nation", "Unknown"),
            "position": match.get("position", "Unknown"),
            "platform": platform,
            "price": price,
            "trend": trend,
            "range": price_range,
            "updated": updated,
            "image": f"https://cdn.futbin.com/content/fifa25/img/players/{match['id']}.png"
        }

    except Exception as e:
        log.error(f"[SCRAPE FAIL] {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch price data")

# --- Graph endpoint ---
@app.get("/api/pricegraph")
def pricegraph(player_name: str = Query(...)):
    """Return PNG price graph for given player."""
    match = next((p for p in PLAYERS if f"{p['name']} {p['rating']}".lower() == player_name.lower()), None)
    if not match:
        raise HTTPException(status_code=404, detail="Player not found")

    price_data = fetch_price_data(match["url"])
    graph = generate_price_graph(price_data, match["name"])
    if not graph:
        raise HTTPException(status_code=404, detail="No graph available")

    return StreamingResponse(graph, media_type="image/png")
