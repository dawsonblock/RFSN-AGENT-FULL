"""RFSN Orchestrator Service (Modular Entrypoint)."""

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.orchestrator.api_routes import api_router, _KERNEL
from services.orchestrator.github_routes import github_router
from services.orchestrator import (
    api_routes,
)  # Need modifying the module/global directly
from system.event_bus import get_event_bus
from services.consensus.deterministic_consensus import ConsensusNode
from rfsn_kernel.kernel import HardKernel

# Configuration
VERSION = "6.4.0-decomposed"
DEV_MODE = os.getenv("RFSN_DEV_MODE", "0") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print(f"INFO: Starting Orchestrator {VERSION}")

    # Init Event Bus
    bus = get_event_bus()

    # Init Consensus
    consensus_node = ConsensusNode("orchestrator-1")

    # Init Kernel (if required)
    try:
        ledger_path = os.getenv("RFSN_HARD_LEDGER_PATH", "/data/kernel_ledger.jsonl")
        kernel = HardKernel(ledger_path=ledger_path)
        # Inject kernel into routes module globals (simple dependency injection)
        api_routes._KERNEL = kernel
        print("INFO: Hard Kernel initialized.")
    except Exception as e:
        print(f"WARN: Hard Kernel failed to init: {e}")
        if not DEV_MODE:
            raise e

    yield

    # Shutdown
    print("INFO: Shutting down Orchestrator.")


def create_app() -> FastAPI:
    app = FastAPI(title="RFSN Orchestrator", version=VERSION, lifespan=lifespan)

    # Middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routes
    app.include_router(api_router)
    app.include_router(github_router)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
