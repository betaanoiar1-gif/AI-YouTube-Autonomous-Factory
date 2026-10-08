"""Provider-neutral discovery types and the DiscoveryProvider contract.

Normalized discovery models live here (not in any vendor package) so that
business logic and the :class:`~factory.providers.base.DiscoveryProvider`
contract never depend on a specific vendor's wire format — the same boundary
discipline as the LLM provider layer.
"""

from factory.providers.discovery.types import (
    DiscoveredChannelItem,
    DiscoveredVideoItem,
    DiscoveryPage,
    DiscoveryQuery,
)

__all__ = [
    "DiscoveredChannelItem",
    "DiscoveredVideoItem",
    "DiscoveryPage",
    "DiscoveryQuery",
]
