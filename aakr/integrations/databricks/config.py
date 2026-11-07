"""Databricks adapter configuration.

Configuration for the DatabricksCatalogAdapter. Validates required
settings and rejects unsupported operations.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DatabricksConfig:
    catalog: str = "oneshot_dev"
    schema: str = "knowledge"
    warehouse_id: str | None = None  # SQL warehouse for interactive transactions
    use_serverless: bool = True  # Use BEGIN ATOMIC on serverless compute

    @property
    def full_schema(self) -> str:
        return f"{self.catalog}.{self.schema}"

    def validate(self) -> None:
        if not self.catalog:
            raise ValueError("catalog is required")
        if not self.schema:
            raise ValueError("schema is required")
        if not self.use_serverless and not self.warehouse_id:
            raise ValueError("warehouse_id is required for interactive transactions")
