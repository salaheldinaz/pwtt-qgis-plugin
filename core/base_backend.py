# -*- coding: utf-8 -*-
"""Abstract base class defining the backend interface for PWTT processing."""

from abc import ABC, abstractmethod
from typing import Optional, Callable, Tuple


class ProductsOfflineError(Exception):
    """Raised when all available products are in cold storage and staging orders were triggered."""

    def __init__(self, message, product_ids=None):
        super().__init__(message)
        self.product_ids = product_ids or []


class PWTTBackend(ABC):
    """All backends (openEO, GEE, local) implement this contract.

    Subclasses must implement:
        name (property): Human-readable backend label.
        id (property): Short identifier ('openeo', 'gee', 'local').
        authenticate(credentials): Validate credentials, return True on success.
        run(...): Execute the PWTT pipeline and write a GeoTIFF.

    After run() completes, ``run_metadata`` contains processing details
    (scenes used, date ranges, etc.) for inclusion in the job metadata file.
    """

    run_metadata: dict = None  # populated by run()

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
