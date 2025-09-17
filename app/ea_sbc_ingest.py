from typing import Any, Dict, List, Optional
import asyncpg

FORMATION_POSITIONS = {
    "f343":  ["GK","RB","CB","CB","LB","RM","CM","LM","RW","ST","LW"],
    "f3142": ["GK","CB","CB","CB","RWB","LWB","CM","CM","CAM","ST","ST"],
    "f4141": ["GK","RB","CB","CB","LB","CDM","RM","CM","LM","CAM","ST"],
    "_default": ["GK","RB","CB","CB","LB","CM","CM","CAM","RW","ST","LW"],
}

def _positions_for(formation: Optional[str]) -> List[str]:
    if not formation:
        return FORMATION_POSITIONS["_default"]
    return FORMATION_POSITIONS.get(formation.lower(), FORMATION_POSITIONS["_default"])

def _try_int(x: Any) -> Optional[int]:
    try:
        return int(x) if x is not None else None
    except:
        return None

def derive_basics_from_elg(elg_req: List[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    out = {
        "min_squad_rating": None,
        "min_chem": None,
        "min_leagues": None,
        "min_nations": None,
        "min_clubs": None,
        "exact_bronze": None,
        "exact_silver": None,
        "exact_gold": None,
        "allow_alt_pos": True,
    }
    for r in elg_req or []:
        t = (r.get("type") or "").upper()
        v = _try_int(r.get("eligibilityValue"))
        if t == "TEAM_RATING_1_TO_100":
            out["min_squad_rating"] = v
        elif t == "CHEMISTRY_POINTS":
            out["min_chem"] = v
        elif t == "LEAGUE_COUNT":
            out["min_leagues"] = v
        elif t == "NATION_COUNT":
            out["min_nations"] = v
        # SAME_* and PLAYER_QUALITY kept in constraints_json for later
    return out

UPSERT_SQL = """
INSERT INTO sbc_challenges (
  challenge_code,
  set_id,
  name,
  positions,
  min_squad_rating,
  min_chem,
  min_nations,
  min_leagues,
  min_clubs,
  exact_bronze,
  exact_silver,
  exact_gold,
  allow_alt_pos,
  formation,
  constraints_json,
  updated_at
)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15, now())
ON CONFLICT (challenge_code) DO UPDATE SET
  set_id            = EXCLUDED.set_id,
  name              = EXCLUDED.name,
  positions         = EXCLUDED.positions,
  min_squad_rating  = EXCLUDED.min_squad_rating,
  min_chem          = EXCLUDED.min_chem,
  min_nations       = EXCLUDED.min_nations,
  min_leagues       = EXCLUDED.min_leagues,
  min_clubs         = EXCLUDED.min_clubs,
  exact_bronze      = EXCLUDED.exact_bronze,
  exact_silver      = EXCLUDED.exact_silver,
  exact_gold        = EXCLUDED.exact_gold,
  allow_alt_pos     = EXCLUDED.allow_alt_pos,
  formation         = EXCLUDED.formation,
  constraints_json  = EXCLUDED.constraints_json,
  updated_at        = now();
"""

async def upsert_set_challenges(con, set_id: int, payload: Dict[str, Any]) -> int:
    arr = payload.get("challenges") or []
    n = 0
    for ch in arr:
        code  = str(ch.get("challengeId") or ch.get("id"))
        name  = ch.get("name") or ""
        formation = (ch.get("formation") or "").lower() or None
        positions = _positions_for(formation)
        basics = derive_basics_from_elg(ch.get("elgReq") or [])

        constraints = {
            "elgReq": ch.get("elgReq") or [],
            "elgOperation": ch.get("elgOperation"),
            "description": ch.get("description"),
            "repeatable": ch.get("repeatable"),
            "status": ch.get("status"),
            "priority": ch.get("priority"),
            "awards": ch.get("awards"),
            "challengeImageId": ch.get("challengeImageId"),
            "endTime": ch.get("endTime"),
            "formation": ch.get("formation"),
        }

        await con.execute(
            UPSERT_SQL,
            code,
            set_id,
            name,
            positions,
            basics["min_squad_rating"],
            basics["min_chem"],
            basics["min_nations"],
            basics["min_leagues"],
            basics["min_clubs"],
            basics["exact_bronze"],
            basics["exact_silver"],
            basics["exact_gold"],
            basics["allow_alt_pos"],
            formation,
            constraints,
        )
        n += 1
    return n
