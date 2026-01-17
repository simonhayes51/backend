from typing import Any, Dict, Optional
import asyncpg

UPSERT_CATEGORY_SQL = """
INSERT INTO sbc_categories (category_id, name, priority, updated_at)
VALUES ($1,$2,$3, now())
ON CONFLICT (category_id) DO UPDATE SET
  name = EXCLUDED.name,
  priority = EXCLUDED.priority,
  updated_at = now();
"""

UPSERT_SET_SQL = """
INSERT INTO sbc_sets (
  set_id, category_id, name, description, challenges_count, hidden,
  repeatable, repeatability_mode, challenges_completed, end_time,
  release_time, set_image_id, awards_json, meta_json, updated_at
)
VALUES (
  $1,$2,$3,$4,$5,$6,
  $7,$8,$9,$10,
  $11,$12,$13,$14, now()
)
ON CONFLICT (set_id) DO UPDATE SET
  category_id          = EXCLUDED.category_id,
  name                 = EXCLUDED.name,
  description          = EXCLUDED.description,
  challenges_count     = EXCLUDED.challenges_count,
  hidden               = EXCLUDED.hidden,
  repeatable           = EXCLUDED.repeatable,
  repeatability_mode   = EXCLUDED.repeatability_mode,
  challenges_completed = EXCLUDED.challenges_completed,
  end_time             = EXCLUDED.end_time,
  release_time         = EXCLUDED.release_time,
  set_image_id         = EXCLUDED.set_image_id,
  awards_json          = EXCLUDED.awards_json,
  meta_json            = EXCLUDED.meta_json,
  updated_at           = now();
"""

def _bool(x: Any) -> Optional[bool]:
    if x is None: return None
    if isinstance(x, bool): return x
    if isinstance(x, (int, float)): return bool(x)
    s = str(x).strip().lower()
    if s in ("true","t","1","yes","y"): return True
    if s in ("false","f","0","no","n"): return False
    return None

def _int(x: Any) -> Optional[int]:
    try:
        return int(x) if x is not None else None
    except:
        return None

async def upsert_sets_payload(con, payload: Dict[str, Any]) -> int:
    categories = payload.get("categories") or []
    total_sets = 0

    for cat in categories:
        cat_id = _int(cat.get("categoryId"))
        cat_name = cat.get("name") or ""
        cat_pri  = _int(cat.get("priority"))
        if cat_id is not None:
            await con.execute(UPSERT_CATEGORY_SQL, cat_id, cat_name, cat_pri)

        for s in (cat.get("sets") or []):
            set_id = _int(s.get("setId"))
            if set_id is None:
                continue

            name        = s.get("name") or ""
            desc        = s.get("description") or ""
            ccnt        = _int(s.get("challengesCount"))
            hidden      = _bool(s.get("hidden"))
            repeatable  = _bool(s.get("repeatable"))
            rep_mode    = s.get("repeatabilityMode")
            c_done      = _int(s.get("challengesCompletedCount"))
            end_time    = _int(s.get("endTime"))
            release     = _int(s.get("releaseTime"))
            image_id    = s.get("setImageId")
            awards      = s.get("awards") or []
            # capture everything else for traceability
            meta = {
                k: v for k, v in s.items()
                if k not in {
                    "setId","name","description","categoryId","challengesCount",
                    "hidden","repeatable","repeatabilityMode","challengesCompletedCount",
                    "endTime","releaseTime","setImageId","awards"
                }
            }

            await con.execute(
                UPSERT_SET_SQL,
                set_id,
                cat_id,
                name,
                desc,
                ccnt,
                hidden,
                repeatable,
                rep_mode,
                c_done,
                end_time,
                release,
                image_id,
                awards,
                meta,
            )
            total_sets += 1

    return total_sets
