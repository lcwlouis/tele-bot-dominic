"""Run a LAN-only studio instance from its private TOML file."""

import argparse
import logging
from pathlib import Path
import subprocess

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from .config import load, read_token
from .http import create_app
from .memory import DarwinMemory
from .service import Studio
from .sources import Sources


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    config = load(args.config)
    interfaces = subprocess.check_output(["/sbin/ifconfig"], text=True, timeout=5)
    own_addresses = [line.split()[1] for line in interfaces.splitlines()
                     if line.lstrip().startswith(("inet ", "inet6 "))]
    sources = Sources(config, own_addresses)
    studio = Studio(config, DarwinMemory())
    app = create_app(studio, sources, read_token(config.token_file))
    logging.basicConfig(level=logging.INFO)

    class DirectPeerProtocol(H11Protocol):
        def connection_made(self, transport):
            if not sources.permits(transport.get_extra_info("peername")):
                logging.getLogger("studio").info("refused connection from unlisted peer")
                transport.abort()
                return
            super().connection_made(transport)

        def connection_lost(self, exc):
            if self.transport is not None:
                super().connection_lost(exc)

    uvicorn.run(app, host=config.bind_host, port=config.port, http=DirectPeerProtocol,
                proxy_headers=False, access_log=False, ws="none", timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
