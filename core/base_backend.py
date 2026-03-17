# -*- coding: utf-8 -*-
"""Abstract base class defining the backend interface for PWTT processing."""

from abc import ABC, abstractmethod
from typing import Optional, Callable, Tuple


class PWTTBackend(ABC):
    """All backends (openEO, GEE, local) implement this contract.

    Subclasses must implement:
        name (property): Human-readable backend label.
        id (property): Short identifier ('openeo', 'gee', 'local').
        authenticate(credentials): Validate credentials, return True on success.
        run(...): Execute the PWTT pipeline and write a GeoTIFF.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def id(self) -> str:
        ...

    @abstractmethod
    def authenticate(self, credentials: dict) -> bool:
        ...

    @abstractmethod
    def run(
        self,
        aoi_wkt: str,
        war_start: str,
        inference_start: str,
        pre_interval: int,
        post_interval: int,
        output_path: str,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        include_footprints: bool = False,
        footprints_path: Optional[str] = None,
    ) -> str:
        """Run PWTT and write result GeoTIFF. Returns path to output file."""
        ...

    def check_dependencies(self) -> Tuple[bool, str]:
        """Return (ok, message). If ok is False, message explains what to install."""
        return True, ""
