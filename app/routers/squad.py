from fastapi import APIRouter

router = APIRouter()

# NOTE: This router previously exposed /search-players and /prices, but both
# were dead code: /search-players was shadowed by the inline handler in
# main.py (registered before this router is include_router()'d), and /prices
# relied on a fut.gg scraping fallback that fut.gg now blocks with a 403.
# Both were removed. If/when this router grows real squad-building or
# chemistry endpoints, they belong here.
