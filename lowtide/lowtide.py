"""Non-Qt plugin base: settings, data directory, network selection."""
import os
from typing import TYPE_CHECKING, Optional

from electrum import constants
from electrum.plugin import BasePlugin
from electrum.util import make_dir

if TYPE_CHECKING:
    from electrum.simple_config import SimpleConfig

MEMPOOL_ONION = 'http://mempoolhqx4isw62xs7abwphsq7ldayuidyx2v2oethdhhj6mlo2r6ad.onion'
MEMPOOL_CLEARNET = 'https://mempool.space'


def network_path_prefix() -> str:
    """mempool.space serves test networks under a path prefix."""
    net = constants.net
    if net is constants.BitcoinMainnet:
        return ''
    if net is constants.BitcoinTestnet4:
        return '/testnet4'
    if net is constants.BitcoinSignet:
        return '/signet'
    if net is constants.BitcoinTestnet:
        return '/testnet'
    return ''


class LowTidePlugin(BasePlugin):

    def __init__(self, parent, config: 'SimpleConfig', name: str):
        BasePlugin.__init__(self, parent, config, name)
        self.data_dir = os.path.join(config.electrum_path(), 'lowtide')
        make_dir(self.data_dir)
        self.logger.info(f"LowTide data dir: {self.data_dir}")

    # --- settings -----------------------------------------------------

    def mempool_base_url(self, *, is_tor: Optional[bool] = None) -> str:
        """Base URL (no trailing slash) for the mempool REST API, including the network prefix."""
        url = (self.config.LOWTIDE_MEMPOOL_URL or MEMPOOL_CLEARNET).rstrip('/')
        if is_tor and url == MEMPOOL_CLEARNET:
            url = MEMPOOL_ONION
        return url + network_path_prefix()

    def accelerator_base_url(self, *, is_tor: Optional[bool] = None) -> str:
        """The accelerator only exists on mempool.space mainnet."""
        base = MEMPOOL_ONION if is_tor else MEMPOOL_CLEARNET
        return base + '/api/v1/services'

    def accelerator_available(self) -> bool:
        return constants.net is constants.BitcoinMainnet

    def notifications_enabled(self) -> bool:
        return bool(self.config.LOWTIDE_NOTIFICATIONS)

    def demo_mode(self) -> bool:
        return bool(self.config.LOWTIDE_DEMO_MODE)
