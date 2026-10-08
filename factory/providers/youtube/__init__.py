"""YouTube Data API v3 discovery provider (Phase 1: intelligence plane).

Production implementation of the
:class:`~factory.providers.base.DiscoveryProvider` contract using the
**documented YouTube Data API v3 only** (``search`` / ``videos`` /
``channels`` list endpoints, API-key auth). No scraping, no browser
automation, no undocumented endpoints, no quota circumvention.
"""

from factory.providers.youtube.client import YouTubeClient
from factory.providers.youtube.provider import YouTubeDiscoveryProvider
from factory.providers.youtube.quota import QUOTA_COSTS, QuotaTracker

__all__ = [
    "QUOTA_COSTS",
    "QuotaTracker",
    "YouTubeClient",
    "YouTubeDiscoveryProvider",
]
