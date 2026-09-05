"""Small helpers shared by the live services (not the seed generator)."""
import asyncio
import json
import logging
import os


def load_json(path: str):
    with open(path) as f:
        return json.load(f)


async def wait_for_files(paths, timeout=120, poll_interval=1.0):
    """Block until every path in `paths` exists, or raise TimeoutError.
    Used at service startup so container start order doesn't have to be
    perfectly enforced by the compose file's depends_on alone -- podman-
    compose's support for `condition: service_completed_successfully`
    varies by version, so this is the belt-and-braces fallback."""
    waited = 0.0
    missing = [p for p in paths if not os.path.exists(p)]
    while missing:
        if waited >= timeout:
            raise TimeoutError(f"timed out waiting for seed files: {missing}")
        logging.getLogger(__name__).info("waiting for seed data: %s", missing)
        await asyncio.sleep(poll_interval)
        waited += poll_interval
        missing = [p for p in paths if not os.path.exists(p)]


def setup_logging(name: str):
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format=f"%(asctime)s {name} %(levelname)s %(message)s",
    )
    return logging.getLogger(name)
