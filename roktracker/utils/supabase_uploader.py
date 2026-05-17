"""Supabase uploader — syncs governor scan data to kd1819.com."""
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SUPABASE_URL = "https://obuzhvvpikdovrufwapk.supabase.co"
STORAGE_BUCKET = "avatars"

_client = None
# Pre-fetched at scan start: gov_id -> {starting_kp, starting_deads, starting_power}
_player_cache: dict = {}


def init(service_role_key: str) -> bool:
    global _client, _player_cache
    try:
        from supabase import create_client
        _client = create_client(SUPABASE_URL, service_role_key)
        result = _client.table("players").select(
            "id,starting_kp,starting_deads,starting_power"
        ).execute()
        _player_cache = {row["id"]: row for row in (result.data or [])}
        logger.info(f"Supabase ready — {len(_player_cache)} existing players cached")
        return True
    except Exception as e:
        logger.error(f"Supabase init failed: {e}")
        _client = None
        return False


def is_ready() -> bool:
    return _client is not None


def _to_int(value: str) -> Optional[int]:
    try:
        v = int(value)
        return v if v >= 0 else None
    except (ValueError, TypeError):
        return None


def upload_governor(
    gov_id: str,
    name: str,
    power: str,
    kill_points: str,
    deads: str,
    acclaim: str,
    alliance: str,
    rank: int,
    profile_image_path: Optional[Path] = None,
) -> None:
    """Upsert one governor into Supabase and optionally upload their avatar."""
    if not _client or not gov_id or not gov_id.isdigit():
        return

    existing = _player_cache.get(gov_id, {})
    kp = _to_int(kill_points)
    pw = _to_int(power)
    dd = _to_int(deads)
    ac = _to_int(acclaim)

    row: dict = {"id": gov_id, "name": name, "rank": rank}

    if alliance and alliance not in ("Skipped", "Unknown", ""):
        row["alliance"] = alliance.strip()

    if pw is not None:
        row["power"] = pw
        row["power_delta"] = pw - existing.get("starting_power", pw)
        if "starting_power" not in existing:
            row["starting_power"] = pw

    if kp is not None:
        row["kill_points"] = kp
        row["kp_delta"] = kp - existing.get("starting_kp", kp)
        if "starting_kp" not in existing:
            row["starting_kp"] = kp

    if dd is not None:
        row["deads"] = dd
        row["deads_delta"] = dd - existing.get("starting_deads", dd)
        if "starting_deads" not in existing:
            row["starting_deads"] = dd

    if ac is not None:
        row["acclaim"] = ac

    # Defaults for players appearing for the first time
    if not existing:
        row.setdefault("strikes", 0)
        row.setdefault("progress", 0)
        row.setdefault("kp_progress", 0)
        row.setdefault("deads_progress", 0)
        row.setdefault("required_kp", 0)
        row.setdefault("required_deads", 0)
        row.setdefault("kp_delta", 0)
        row.setdefault("deads_delta", 0)
        row.setdefault("power_delta", 0)

    # Upload avatar to Supabase Storage
    if profile_image_path and profile_image_path.exists():
        try:
            with open(profile_image_path, "rb") as f:
                data = f.read()
            _client.storage.from_(STORAGE_BUCKET).upload(
                path=f"{gov_id}.jpg",
                file=data,
                file_options={"content-type": "image/jpeg", "upsert": "true"},
            )
            row["photo_url"] = (
                f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/{gov_id}.jpg"
            )
        except Exception as e:
            logger.warning(f"Avatar upload failed for {gov_id}: {e}")

    try:
        _client.table("players").upsert(row, on_conflict="id").execute()
        _player_cache[gov_id] = {**existing, **row}
    except Exception as e:
        logger.error(f"Supabase upsert failed for {gov_id}: {e}")
