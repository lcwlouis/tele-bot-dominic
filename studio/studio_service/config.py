"""Private, local configuration for the studio host."""

import ipaddress
import os
from pathlib import Path
import stat
import tomllib
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

GIB = 1024**3
MIB = 1024**2


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Outfit(Settings):
    portrait: Path
    turnaround: Path
    # What the outfit looks like, in words, for `python -m studio_service.portraits generate`.
    description: str = ""


class Desktop(Settings):
    max_containers: Annotated[int, Field(ge=0)] = 0
    container_memory_mb: Annotated[int, Field(ge=0)] = 0
    min_free_memory_mb: Annotated[int, Field(ge=0)] = 0


class WorkerConfig(Settings):
    command: Annotated[list[str], Field(min_length=1)]
    cwd: Path | None = None


class Workers(Settings):
    voice: WorkerConfig
    image: WorkerConfig
    video: WorkerConfig
    special: WorkerConfig


class Config(Settings):
    bind_host: str
    port: Annotated[int, Field(ge=1, le=65535)]
    token_file: Path
    allowed_sources: Annotated[list[str], Field(min_length=1)]
    # Additional locally configured VM/bridge CIDRs and interface addresses.
    denied_sources: list[str] = Field(default_factory=list)
    ffmpeg: Path
    workers: Workers
    default_outfit: str
    outfits: dict[str, Outfit]
    desktop: Desktop = Field(default_factory=Desktop)

    @property
    def reserve(self) -> int:
        desktop = self.desktop
        return 16 * GIB + MIB * (
            desktop.max_containers * desktop.container_memory_mb + desktop.min_free_memory_mb
        )


def read_token(path: Path) -> bytes:
    with path.open("rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("token file must be a regular file with mode 0600")
        token = stream.read(1025).strip()
    if not 32 <= len(token) <= 1024 or any(byte < 33 or byte > 126 for byte in token):
        raise ValueError("invalid token file")
    return token


def load(path: Path) -> Config:
    with path.open("rb") as stream:
        config = Config.model_validate(tomllib.load(stream))
    host = ipaddress.ip_address(config.bind_host)
    if host.is_unspecified or host.is_loopback or host.is_multicast:
        raise ValueError("bind_host must be a specific LAN interface address")
    if config.default_outfit not in config.outfits:
        raise ValueError("default_outfit must name a configured outfit")
    return config
