"""Topic-agnostic curator substrate.

Public surface (re-exported here for convenience; each module is also
importable directly):
  - Source, Item       — curator.core.types
"""

from curator.core.types import Item, Source

__all__ = ["Source", "Item"]
