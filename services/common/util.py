"""Small helpers shared by the live services (not the seed generator)."""
import asyncio
import contextlib
import json
import logging
import os
import signal


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


async def serve_until_signal(server, log, drain_timeout=25):
    """Runs an asyncio.start_server server until SIGTERM/SIGINT, then stops
    accepting *new* connections and gives already-accepted ones (each its
    own task) up to `drain_timeout` seconds to finish normally before the
    process exits. Used by the gateway and issuer-simulator so a plain
    `podman stop`/`podman-compose stop` completes a transaction it already
    accepted -- leaving its DB row and dashboard event in a finished state
    -- instead of abandoning it mid-flight and forcing a SIGKILL after
    compose's stop grace period."""
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    this_task = asyncio.current_task()
    async with server:
        serve_task = asyncio.create_task(server.serve_forever())
        await stop_event.wait()

        log.info("shutdown signal received: no longer accepting new connections, draining in-flight ones")
        server.close()
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task

        in_flight = [t for t in asyncio.all_tasks() if t not in (this_task, serve_task)]
        if in_flight:
            _done, pending = await asyncio.wait(in_flight, timeout=drain_timeout)
            if pending:
                log.warning("%d in-flight connection(s) still running after %ss, exiting anyway",
                            len(pending), drain_timeout)
        log.info("shutdown complete")
