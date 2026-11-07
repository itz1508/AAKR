"""AAKR Integrations: provider-specific adapters and registration."""


def register_builtin_providers():
    """Register built-in storage providers with the core registry.

    Import errors are NOT suppressed — if a provider module fails to import,
    the error propagates so the caller can diagnose the issue.
    """
    from aakr.core.registry import register_provider
    from aakr.integrations.sqlite.adapter import SQLiteCatalogAdapter

    register_provider("sqlite", lambda **c: SQLiteCatalogAdapter(**c))
    from aakr.integrations.databricks.adapter import DatabricksCatalogAdapter

    register_provider("databricks", lambda **c: DatabricksCatalogAdapter(**c))


# Auto-register on import
register_builtin_providers()
