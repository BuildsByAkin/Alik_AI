"""Run the five connections passes in order, in-process, with logging ON so the PASS_SUMMARY
lines are visible. These are the SAME functions the connections-* console scripts call — this
just sequences them in one process and turns on INFO logging.

Runs in the connections venv (imports connections_service). Reads services/connections/.env:
  uv run --directory services/connections python <abs>/scripts/connections_stress/run_passes.py
"""

from __future__ import annotations

import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from connections_service.auth_client import AuthClient  # noqa: E402
from connections_service.brain_client import BrainClient  # noqa: E402
from connections_service.cluster import clustering_pass  # noqa: E402
from connections_service.config import settings  # noqa: E402
from connections_service.eval import eval_pass  # noqa: E402
from connections_service.ingest import run_ingest  # noqa: E402
from connections_service.interests import all_interest_nodes  # noqa: E402
from connections_service.llm import AnthropicLLM  # noqa: E402
from connections_service.scoring import scoring_pass  # noqa: E402
from connections_service.store import PgStore  # noqa: E402
from connections_service.surface import surface_pass  # noqa: E402


async def _run() -> None:
    token = settings.service_token.get_secret_value()
    store = await PgStore.connect(settings.database_url)
    await store.ensure_interest_nodes(all_interest_nodes())
    brain = BrainClient(base_url=settings.brain_url, service_token=token)
    auth = AuthClient(base_url=settings.auth_url, service_token=token)
    llm = AnthropicLLM(
        api_key=settings.anthropic_api_key.get_secret_value(),
        model=settings.eval_model,
        max_tokens=settings.eval_max_tokens,
    )
    try:
        print("\n--- PASS 1: ingest ---")
        print(await run_ingest(store, brain, auth, settings))
        print("\n--- PASS 2: score ---")
        print(await scoring_pass(store, settings))
        print("\n--- PASS 3: eval (LLM) ---")
        print(await eval_pass(store, llm, settings))
        print("\n--- PASS 4: surface (1:1) ---")
        print(await surface_pass(store, brain, settings))
        print("\n--- PASS 5: cluster (groups) ---")
        print(await clustering_pass(store, brain, settings))
    finally:
        await brain.aclose()
        await auth.aclose()
        await store.aclose()


if __name__ == "__main__":
    asyncio.run(_run())
