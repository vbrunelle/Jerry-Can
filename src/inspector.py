"""Abstract and concrete inspectors for Jerry-Can data stores."""

from abc import ABC, abstractmethod
from typing import Any


class Inspector(ABC):
    """Abstract base class for data store inspectors."""

    @abstractmethod
    def show_schema(self) -> None:
        """Print the schema of the underlying data store."""

    @abstractmethod
    def show_summary(self) -> None:
        """Print a high-level summary (row counts, snapshot range, …)."""

    @abstractmethod
    def show_latest_prices(self, limit: int = 20) -> None:
        """Print the lowest prices from the most recent snapshot."""

    @abstractmethod
    def show_regions(self) -> None:
        """Print station counts grouped by region."""

    @abstractmethod
    def show_snapshots(self) -> None:
        """Print the most recent snapshots with record counts."""

    def show_all(self) -> None:
        """Convenience method — run every report in order."""
        self.show_schema()
        self.show_summary()
        self.show_snapshots()
        self.show_regions()
        self.show_latest_prices()
