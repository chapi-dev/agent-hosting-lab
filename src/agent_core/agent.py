"""The trip planner agent.

There is exactly one definition of the agent in this repository and all four
deployments import it from here. That is deliberate. If the self-hosted and the
hosted variants ran different code, any difference we measured later would be
uninterpretable - you could always blame the code. By sharing this module the
only independent variable left in the experiment is the hosting model.

The agent keeps an itinerary in session state and exposes three tools over it.
State is functional here, not decorative: `list_trip` can only answer correctly
if the writes from previous turns survived. When session state breaks, the agent
does not crash - it cheerfully gives you a wrong answer, which is exactly how
this class of bug behaves in production and exactly why it is worth measuring.
"""

from __future__ import annotations

import os
from typing import Annotated

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential

from .state import ItineraryStore

INSTRUCTIONS = """You are Trip Planner, a concise travel assistant.

You maintain an itinerary for the user across the conversation. Always use your
tools to read or change the itinerary - never rely on what you remember from
earlier messages, because the itinerary is the source of truth and it may have
been changed by something other than this conversation.

When the user asks what is in their trip, call list_trip and report exactly what
it returns, in order. If it returns nothing, say the trip is empty. Do not
invent destinations and do not apologise at length. Keep answers to one or two
sentences.
"""


def build_tools(store: ItineraryStore, session_id: str) -> list:
    """Binds the three itinerary tools to one session.

    The tools close over the session id rather than taking it as a parameter, so
    the model cannot pass the wrong one and cannot read another user's trip by
    guessing an identifier. Tool arguments come from the model and are therefore
    untrusted; the session id comes from the transport and is not.
    """

    async def add_destination(
        destination: Annotated[str, "City or place to add, e.g. 'Paris'"],
    ) -> str:
        """Adds a destination to the end of the user's itinerary."""
        itinerary = await store.load(session_id)
        name = destination.strip()
        if not name:
            return "No destination given."
        if name.lower() in (d.lower() for d in itinerary.destinations):
            return f"{name} is already in the trip."
        itinerary.destinations.append(name)
        await store.save(session_id, itinerary)
        return f"Added {name}. The trip now has {len(itinerary.destinations)} stop(s)."

    async def list_trip() -> str:
        """Returns the destinations currently in the user's itinerary, in order."""
        itinerary = await store.load(session_id)
        if not itinerary.destinations:
            return "The itinerary is empty."
        return " -> ".join(itinerary.destinations)

    async def remove_destination(
        destination: Annotated[str, "City or place to remove"],
    ) -> str:
        """Removes a destination from the user's itinerary."""
        itinerary = await store.load(session_id)
        match = next(
            (d for d in itinerary.destinations if d.lower() == destination.strip().lower()),
            None,
        )
        if match is None:
            return f"{destination} is not in the trip."
        itinerary.destinations.remove(match)
        await store.save(session_id, itinerary)
        return f"Removed {match}."

    async def describe_runtime() -> str:
        """Reports which platform facilities are present in this environment.

        Not a travel tool. It exists so the lab can ask the running agent what
        the platform actually gave it, rather than trusting documentation or
        assumptions - one of which (an auto-injected project endpoint) turned
        out to be wrong and cost a failed deploy.

        Only the presence of each variable is reported, never its value, so this
        is safe to call from a chat client.
        """
        import os as _os

        interesting = [
            "APPLICATIONINSIGHTS_CONNECTION_STRING",
            "AZURE_AI_PROJECT_ENDPOINT",
            "PROJECT_ENDPOINT",
            "AZURE_CLIENT_ID",
            "AZURE_TENANT_ID",
            "AZURE_FEDERATED_TOKEN_FILE",
            "MSI_ENDPOINT",
            "IDENTITY_ENDPOINT",
            "CONTAINER_APP_REPLICA_NAME",
            "STATE_BACKEND",
            "HOME",
        ]
        present = [k for k in interesting if _os.environ.get(k)]
        absent = [k for k in interesting if not _os.environ.get(k)]

        # The platform reserves the FOUNDRY_ and AGENT_ prefixes. Listing the
        # names it actually sets is the only way to tell an absent value from
        # one that arrived under a name we were not looking for.
        reserved = sorted(
            k for k in _os.environ if k.startswith(("FOUNDRY_", "AGENT_"))
        )
        return (
            f"present={','.join(present) or 'none'}; "
            f"absent={','.join(absent) or 'none'}; "
            f"reserved_prefix_names={','.join(reserved) or 'none'}; "
            f"home={_os.path.expanduser('~')}"
        )

    return [add_destination, list_trip, remove_destination, describe_runtime]


def build_chat_client() -> FoundryChatClient:
    """Creates the chat client from environment variables.

    The two hosting models supply the endpoint differently, which is itself a
    difference worth knowing about:

    - Hosted: the platform injects FOUNDRY_PROJECT_ENDPOINT. Nothing to declare.
    - Self-hosted: you set AZURE_AI_PROJECT_ENDPOINT yourself, in Bicep.

    Preferring the platform value keeps the hosted path free of configuration
    that can drift away from the project it is deployed into.
    """
    endpoint = (
        os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
        or os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
        or os.environ["PROJECT_ENDPOINT"]
    )
    return FoundryChatClient(
        project_endpoint=endpoint,
        model=os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-5.4-mini"),
        credential=DefaultAzureCredential(),
    )


def build_agent(store: ItineraryStore, session_id: str, client=None) -> Agent:
    """Assembles the agent for one session."""
    return Agent(
        client or build_chat_client(),
        INSTRUCTIONS,
        name="trip-planner",
        description="Keeps a travel itinerary across a conversation.",
        tools=build_tools(store, session_id),
    )
