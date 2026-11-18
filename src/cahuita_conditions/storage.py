"""One latest state per configured source. Writes never destroy the last valid snapshot."""

import asyncio
import logging
from pathlib import Path

import aiosqlite

from .models import CacheState

log = logging.getLogger("cahuita.storage")


class CacheStore:
    def __init__(self, path: Path):
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = await aiosqlite.connect(self.path)
            await self.connection.execute("PRAGMA journal_mode=WAL")
            await self.connection.execute("PRAGMA busy_timeout=3000")
            async with self.connection.execute("PRAGMA user_version") as cursor:
                row = await cursor.fetchone()
            if row[0] not in {0, 1}:
                raise RuntimeError("Unsupported cache schema version")
            await self.connection.execute(
                "CREATE TABLE IF NOT EXISTS cache ("
                "location_id TEXT NOT NULL, source TEXT NOT NULL, fingerprint TEXT NOT NULL, "
                "state TEXT NOT NULL, PRIMARY KEY(location_id, source))"
            )
            await self.connection.execute("PRAGMA user_version=1")
            await self.connection.commit()
        except Exception as exc:
            await self.close()
            raise RuntimeError(f"Cannot initialize SQLite cache at {self.path}: {exc}") from exc

    async def load(self, expected: dict[tuple[str, str], str]) -> dict[tuple[str, str], CacheState]:
        async with self.lock:
            async with self.connection.execute(
                "SELECT location_id, source, fingerprint, state FROM cache"
            ) as cursor:
                rows = await cursor.fetchall()
            restored = {}
            for location, source, signature, payload in rows:
                key = (location, source)
                if expected.get(key) == signature:
                    restored[key] = CacheState.model_validate_json(payload)
                else:
                    await self.connection.execute(
                        "DELETE FROM cache WHERE location_id=? AND source=?", key
                    )
            await self.connection.commit()
            return restored

    async def save(self, key: tuple[str, str], signature: str, state: CacheState) -> None:
        async with self.lock:
            try:
                await self.connection.execute(
                    "INSERT INTO cache VALUES (?, ?, ?, ?) ON CONFLICT(location_id, source) "
                    "DO UPDATE SET fingerprint=excluded.fingerprint, state=excluded.state",
                    (*key, signature, state.model_dump_json()),
                )
                await self.connection.commit()
            except Exception:
                await self.connection.rollback()
                raise

    async def healthy(self) -> bool:
        try:
            async with asyncio.timeout(1):
                async with self.lock:
                    async with self.connection.execute("SELECT 1") as cursor:
                        await cursor.fetchone()
            return True
        except Exception:
            log.error("storage_check_failed", exc_info=True)
            return False

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None
