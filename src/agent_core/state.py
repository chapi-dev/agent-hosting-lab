"""Session state for the trip planner agent.

This module is the whole argument of the lab in miniature.

`DiskStore` writes JSON under a directory. That is all it does. Deployed as a
self-hosted container it is a bug: the filesystem is ephemeral, it is not shared
between replicas, and a second replica cannot see what the first one wrote.
Deployed as a Foundry hosted agent the identical code is correct, because the
platform gives each session its own sandbox and persists $HOME across turns and
idle periods.

Same code. Different guarantees. The guarantees come from where you run it.

`CosmosStore` is what self-hosting forces you to write to get the guarantee that
the hosted model hands you for nothing. Compare the two classes and note that
the difference is not just lines of code - it is a dependency, a client
lifecycle, a credential, a role assignment, a partition key decision, a TTL
policy, and a failure mode to handle.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol


class Itinerary(dict):
    """A trip: an ordered list of destinations plus a free-text note."""

    @classmethod
    def empty(cls) -> Itinerary:
        return cls(destinations=[], note="")

    @property
    def destinations(self) -> list[str]:
        return self.setdefault("destinations", [])


class ItineraryStore(Protocol):
    """Anything that can persist an itinerary for a session."""

    backend: str

    async def load(self, session_id: str) -> Itinerary: ...

    async def save(self, session_id: str, itinerary: Itinerary) -> None: ...


class DiskStore:
    """Stores itineraries as files on local disk.

    Correct under Foundry hosted agents, where $HOME is per-session and durable.
    Incorrect under a self-hosted container app with more than one replica.
    """

    backend = "disk"

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or os.environ.get("STATE_DIR") or Path.home() / "sessions")
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:120] or "default"
        return self.root / f"{safe}.json"

    async def load(self, session_id: str) -> Itinerary:
        path = self._path(session_id)
        if not path.exists():
            return Itinerary.empty()
        try:
            return Itinerary(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return Itinerary.empty()

    async def save(self, session_id: str, itinerary: Itinerary) -> None:
        self._path(session_id).write_text(
            json.dumps(itinerary, ensure_ascii=False), encoding="utf-8"
        )


class CosmosStore:
    """Stores itineraries in Azure Cosmos DB.

    This is the self-hosted answer to the problem DiskStore has. Everything in
    this class - the async client, the credential, the partition key, the 404
    handling, the TTL - is work the hosted model does not ask you to do.
    """

    backend = "cosmos"

    def __init__(
        self,
        endpoint: str,
        database: str = "agentstate",
        container: str = "sessions",
        credential=None,
    ) -> None:
        from azure.cosmos.aio import CosmosClient
        from azure.identity.aio import DefaultAzureCredential

        self._credential = credential or DefaultAzureCredential()
        self._client = CosmosClient(endpoint, credential=self._credential)
        self._container = self._client.get_database_client(database).get_container_client(
            container
        )

    async def load(self, session_id: str) -> Itinerary:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            doc = await self._container.read_item(session_id, partition_key=session_id)
        except CosmosResourceNotFoundError:
            return Itinerary.empty()
        return Itinerary(
            destinations=doc.get("destinations", []), note=doc.get("note", "")
        )

    async def save(self, session_id: str, itinerary: Itinerary) -> None:
        await self._container.upsert_item(
            {
                "id": session_id,
                "sessionId": session_id,
                "destinations": itinerary.get("destinations", []),
                "note": itinerary.get("note", ""),
            }
        )

    async def close(self) -> None:
        await self._client.close()
        await self._credential.close()


def store_from_env() -> ItineraryStore:
    """Selects a store from STATE_BACKEND. Defaults to disk.

    The hosted deployment leaves STATE_BACKEND unset and gets DiskStore, which is
    the right answer there. The self-hosted deployments set it explicitly, which
    is the point: self-hosting makes this a decision you have to make and defend.
    """
    backend = (os.environ.get("STATE_BACKEND") or "disk").lower()
    if backend == "cosmos":
        endpoint = os.environ["COSMOS_ENDPOINT"]
        return CosmosStore(endpoint)
    return DiskStore()
