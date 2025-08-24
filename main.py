from bs4 import BeautifulSoup
import requests
import re

# ---------------- PRICE CHECK ENDPOINT ---------------- #
@app.get("/api/pricecheck")
async def price_check(player: str):
    """
    Fetch the latest price for a FUT player from FUT.GG.
    Example: /api/pricecheck?player=Erling Haaland
    """
    try:
        # Format player name into URL-friendly format
        search_name = player.lower().replace(" ", "-")
        url = f"https://www.fut.gg/players/?name={search_name}"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/115.0.0.0 Safari/537.36"
        }

        # Search FUT.GG
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=404, detail="Failed to fetch data from FUT.GG")

        soup = BeautifulSoup(resp.text, "html.parser")

        # Find the first player result block
        player_link = soup.find("a", href=re.compile(r"^/players/\d+-"))
        if not player_link:
            raise HTTPException(status_code=404, detail="Player not found")

        # Build the player's specific page URL
        player_url = f"https://www.fut.gg{player_link['href']}"

        # Fetch player page
        resp = requests.get(player_url, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=404, detail="Failed to fetch player details")

        soup = BeautifulSoup(resp.text, "html.parser")

        # Locate price container
        price_block = soup.find(
            "div",
            class_="font-bold text-2xl flex flex-row items-center gap-1 justify-self-end"
        )

        if not price_block:
            return {"player": player, "price": None, "note": "No BIN price available (SBC/Objective)"}

        # Extract price (remove commas and convert to int)
        raw_price = price_block.get_text(strip=True).replace(",", "")
        price = int(re.sub(r"\D", "", raw_price))

        return {
            "player": player,
            "price": price,
            "platform": "console"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Price check failed: {str(e)}")
