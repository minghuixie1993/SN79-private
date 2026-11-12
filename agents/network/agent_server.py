# SPDX-License-Identifier: MIT
"""
Serves one agent to several local validators, one state at a time.

A miner hands each validator's state to its agent in turn: taos/im/neurons/miner.py calls the synchronous
`agent.handle` from its async forward, so the event loop runs one handle at a time. The plain launcher serves
`handle` as a FastAPI `def` endpoint, which runs on a thread pool, and agents keep per-state attributes such as
`self.accounts`; with several validators posting at once, two states would be handled together and read each
other's accounts. The lock here restores the miner's order.

Usage (run_network.py starts these):
    python agents/network/agent_server.py agents/MyAgentV2.py --port 9100 --agent_id 0 --params lazy_load=1
"""
import argparse
import functools
import importlib.util
import re
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def load_agent_class(path: Path):
    """The class `path` serves when run directly: the argument of its `launch(...)` call."""
    match = re.search(r"^\s*launch\((\w+)\)", path.read_text(), re.MULTILINE)
    if match is None:
        raise SystemExit(f"{path} has no launch(<AgentClass>) call")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return getattr(module, match.group(1))


def main():
    import bittensor as bt
    import uvicorn
    from fastapi import APIRouter, FastAPI
    from taos.common.config import ParseKwargs

    parser = argparse.ArgumentParser()
    parser.add_argument("agent_file")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--agent_id", type=int, required=True)
    parser.add_argument("--params", nargs="*", action=ParseKwargs, default=argparse.Namespace())
    args = parser.parse_args()

    agent_file = Path(args.agent_file).resolve()
    # Agents import their siblings (e.g. shared helpers) as top-level modules, as they do when run directly.
    sys.path.insert(0, str(agent_file.parent))
    agent = load_agent_class(agent_file)(args.agent_id, args.params)

    lock = threading.Lock()
    handle = agent.handle

    @functools.wraps(handle)
    def locked_handle(*a, **kw):
        with lock:
            return handle(*a, **kw)

    # FastAPI reads the request body's type from the signature functools.wraps copied. An agent that overrides
    # handle(self, state) without an annotation (MyAgent10_23_9Gpt, 26 Oct) makes `state` a required QUERY parameter
    # and every request fails with 422 "missing"; take the annotations from the nearest base class that has them.
    import inspect
    sig = inspect.signature(handle)
    if any(p.annotation is inspect.Parameter.empty for p in sig.parameters.values()):
        for base in type(agent).__mro__[1:]:
            base_handle = base.__dict__.get("handle")
            if base_handle is None:
                continue
            import typing
            try:
                hints = typing.get_type_hints(base_handle)        # resolved types, not strings: FastAPI evaluates
            except Exception:                                    # names in THIS module, where they do not exist
                continue
            base_sig = inspect.signature(base_handle)
            params = [p.replace(annotation=hints[n]) for n, p in base_sig.parameters.items() if n != "self" and n in hints]
            if params and len(params) == len([n for n in base_sig.parameters if n != "self"]):
                locked_handle.__signature__ = base_sig.replace(parameters=params, return_annotation=inspect.Signature.empty)
                break

    router = APIRouter()
    router.add_api_route("/handle", locked_handle, methods=["POST"], response_model=None)
    app = FastAPI()
    app.include_router(router)
    # The agent's own routes after ours, so its unlocked /handle is shadowed and anything else it serves stays.
    app.include_router(agent.router)
    bt.logging.set_info()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
