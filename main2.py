from fastapi import FastAPI, Request, HTTPException, Query, Depends
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup
import requests

# --- Keep all your existing imports, middleware, auth, etc. unchanged ---

@app.get("/api/pricecheck")
async def price_check(
    request: Request,
    player_name: str = Query(...),
    platform: str = Query("console"),
    user_id: str = Depends(get_current_user)  # ✅ Keep authentication intact
):
    """
    Fetch player price data from FUTBIN with fuzzy name matching.
    """
    try:
        # Match player by partial lowercase search + rating
        matched_player = next(
            (
                p for p in PLAYERS_DB
                if player_name.lower() in f"{p['name'].lower()} {p['rating']}"
            ),
            None
        )

        if not matched_player:
            raise HTTPException(status_code=404, detail="Player not found")

        player_id = matched_player["id"]
        player_name_clean = matched_player["name"]
        slug = player_name_clean.replace(" ", "-").lower()
        futbin_url = f"https://www.futbin.com/25/player/{player_id}/{slug}"

        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(futbin_url, headers=headers)
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail="Failed to fetch FUTBIN data")

        soup = BeautifulSoup(response.text, "html.parser")
        prices_wrapper = soup.find("div", class_="lowest-prices-wrapper")
        if not prices_wrapper:
            raise HTTPException(status_code=500, detail="Could not find prices on FUTBIN")

        price_elements = prices_wrapper.find_all("div", class_="lowest-price")

        def get_price_text(index):
            if len(price_elements) > index:
                return price_elements[index].text.strip().replace(",", "").replace("\n", "")
            return "0"

        # Console = PS + Xbox combined check
        if platform.lower() == "console":
            ps_price = get_price_text(0)
            xbox_price = get_price_text(1)
            price = ps_price if ps_price != "0" else xbox_price
        elif platform.lower() == "pc":
            price = get_price_text(2)
        else:
            price = "0"

        price = "N/A" if price == "0" or price == "" else f"{int(price):,}"

        return {
            "player": player_name_clean,
            "rating": matched_player["rating"],
            "platform": platform.capitalize(),
            "price": price,
            "source": "FUTBIN"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Price check failed: {str(e)}")


@app.get("/api/players")
async def get_players(user_id: str = Depends(get_current_user)):
    """
    Return a list of all player names + ratings for dashboard autocomplete.
    """
    try:
        names = [f"{p['name']} {p['rating']}" for p in PLAYERS_DB]
        return {"players": names}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch players: {e}")
