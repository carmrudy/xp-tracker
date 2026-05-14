"""
MongoDB persistence for per-guild member XP (Motor async driver).

Documents store:
  - xp: total cumulative XP (source of truth)
  - level: current level (1–20), derived from xp
  - xp_to_next_level: XP still needed to reach the next level (0 at level 20)
"""

from __future__ import annotations

import os

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import ReturnDocument, UpdateOne

import leveling

DEFAULT_DB_NAME = "xp_tracker"
USER_XP_COLLECTION = "user_xp"


def mongo_uri() -> str:
    return os.environ.get("MONGODB_URI", "").strip()


def mongo_db_name() -> str:
    return os.environ.get("MONGO_DB_NAME", DEFAULT_DB_NAME).strip() or DEFAULT_DB_NAME


async def ensure_xp_indexes(collection: AsyncIOMotorCollection) -> None:
    await collection.create_index(
        [("guild_id", 1), ("user_id", 1)],
        unique=True,
        name="guild_user_unique",
    )


async def _persist_derived(
    collection: AsyncIOMotorCollection,
    *,
    guild_id: int,
    user_id: int,
    derived: leveling.DerivedProgress,
) -> None:
    await collection.update_one(
        {"guild_id": guild_id, "user_id": user_id},
        {
            "$set": {
                "level": derived.level,
                "xp_to_next_level": derived.xp_to_next_level,
            }
        },
    )


async def grant_xp(
    collection: AsyncIOMotorCollection,
    *,
    guild_id: int,
    user_id: int,
    amount: int,
) -> leveling.DerivedProgress:
    """
    Add XP to a member in a guild (admin path).

    Persists total ``xp`` plus derived ``level`` and ``xp_to_next_level``.
    Raises ValueError if amount is not positive.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    doc = await collection.find_one_and_update(
        {"guild_id": guild_id, "user_id": user_id},
        {
            "$inc": {"xp": amount},
            "$setOnInsert": {"guild_id": guild_id, "user_id": user_id},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    total = int(doc["xp"])
    derived = leveling.progress_from_total(total)
    await _persist_derived(collection, guild_id=guild_id, user_id=user_id, derived=derived)
    return derived


async def remove_xp(
    collection: AsyncIOMotorCollection,
    *,
    guild_id: int,
    user_id: int,
    amount: int,
) -> tuple[leveling.DerivedProgress, int]:
    """
    Subtract XP for a member (admin path). Total ``xp`` never goes below **0**.

    Returns ``(new_progress, amount_actually_removed)`` where the second value is
    how many XP were removed after clamping (may be less than ``amount`` if the
    user had less XP than requested).
    Raises ValueError if amount is not positive.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    doc = await collection.find_one({"guild_id": guild_id, "user_id": user_id})
    if doc is None:
        zero = leveling.progress_from_total(0)
        return zero, 0
    cur = max(0, int(doc.get("xp", 0) or 0))
    new_total = max(0, cur - amount)
    removed = cur - new_total
    derived = leveling.progress_from_total(new_total)
    await collection.update_one(
        {"guild_id": guild_id, "user_id": user_id},
        {
            "$set": {
                "xp": new_total,
                "level": derived.level,
                "xp_to_next_level": derived.xp_to_next_level,
            }
        },
    )
    return derived, removed


_BULK_WRITE_CHUNK = 500


async def add_xp_to_all_in_guild(
    collection: AsyncIOMotorCollection,
    *,
    guild_id: int,
    amount: int,
) -> int:
    """
    Add ``amount`` XP to every ``user_xp`` document for this guild (same ``guild_id``).

    Recomputes ``level`` and ``xp_to_next_level`` for each row. Returns how many
    documents were updated (0 if there are no records for this server).
    Raises ValueError if ``amount`` is not positive.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    cursor = collection.find({"guild_id": guild_id})
    docs = await cursor.to_list(length=None)
    if not docs:
        return 0
    ops: list[UpdateOne] = []
    for doc in docs:
        cur = int(doc.get("xp", 0) or 0)
        new_total = cur + amount
        d = leveling.progress_from_total(new_total)
        ops.append(
            UpdateOne(
                {"_id": doc["_id"]},
                {
                    "$set": {
                        "xp": new_total,
                        "level": d.level,
                        "xp_to_next_level": d.xp_to_next_level,
                    }
                },
            )
        )
    for i in range(0, len(ops), _BULK_WRITE_CHUNK):
        chunk = ops[i : i + _BULK_WRITE_CHUNK]
        await collection.bulk_write(chunk, ordered=False)
    return len(ops)


async def get_user_progress(
    collection: AsyncIOMotorCollection,
    *,
    guild_id: int,
    user_id: int,
) -> leveling.DerivedProgress:
    """
    Return progress for this guild + user. ``xp`` in the DB is authoritative;
    ``level`` / ``xp_to_next_level`` are recomputed and patched if missing or stale.
    """
    doc = await collection.find_one({"guild_id": guild_id, "user_id": user_id})
    if doc is None or doc.get("xp") is None:
        return leveling.progress_from_total(0)
    total = int(doc["xp"])
    derived = leveling.progress_from_total(total)
    if (
        doc.get("level") != derived.level
        or doc.get("xp_to_next_level") != derived.xp_to_next_level
    ):
        await _persist_derived(
            collection, guild_id=guild_id, user_id=user_id, derived=derived
        )
    return derived


def create_mongo_client(uri: str) -> AsyncIOMotorClient:
    return AsyncIOMotorClient(uri)


def xp_collection(client: AsyncIOMotorClient, db_name: str | None = None) -> AsyncIOMotorCollection:
    db: AsyncIOMotorDatabase = client[db_name or mongo_db_name()]
    return db[USER_XP_COLLECTION]
