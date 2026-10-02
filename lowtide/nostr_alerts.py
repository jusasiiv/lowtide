"""Nostr DM alerts: an encrypted direct message (NIP-04, kind 4) to the user's phone when something happens.

Uses Electrum's bundled electrum_aionostr, Electrum's relays and its proxy settings. The sender key is
generated once and kept in the config. Alerts only go out while Electrum is running.
"""
import asyncio
import ssl
import time
from typing import Callable, List, Optional, TYPE_CHECKING

import electrum_aionostr as aionostr
from electrum_aionostr.key import PrivateKey, PublicKey

from electrum import util
from electrum.network import Network
from electrum.util import ca_path, make_aiohttp_proxy_connector

if TYPE_CHECKING:
    from electrum.simple_config import SimpleConfig

KIND_DM = 4
SEND_TIMEOUT_S = 25


def parse_recipient(text: str) -> Optional[str]:
    """npub… or 64-hex pubkey -> hex pubkey, or None."""
    text = (text or '').strip()
    if not text:
        return None
    if text.lower().startswith('npub'):
        try:
            return PublicKey.from_npub(text).hex()
        except Exception:
            return None
    if len(text) == 64:
        try:
            bytes.fromhex(text)
            return text.lower()
        except ValueError:
            return None
    return None


class NostrAlerts:

    def __init__(self, config: 'SimpleConfig', logger):
        self.config = config
        self.logger = logger
        self._ssl = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=ca_path)

    # --- keys and settings --------------------------------------------

    def privkey_hex(self) -> str:
        k = self.config.LOWTIDE_NOSTR_PRIVKEY
        if not k:
            k = PrivateKey().hex()
            self.config.LOWTIDE_NOSTR_PRIVKEY = k
        return k

    def our_npub(self) -> str:
        return PrivateKey(bytes.fromhex(self.privkey_hex())).public_key.bech32()

    def recipient_hex(self) -> Optional[str]:
        return parse_recipient(self.config.LOWTIDE_NOSTR_NPUB)

    def relays(self) -> List[str]:
        raw = (self.config.LOWTIDE_NOSTR_RELAYS or '').strip() or (self.config.NOSTR_RELAYS or '')
        return [r.strip() for r in raw.split(',') if r.strip()][:8]

    def enabled(self) -> bool:
        return bool(self.config.LOWTIDE_NOSTR_ENABLED) and self.recipient_hex() is not None

    # --- sending --------------------------------------------------------

    async def _send(self, text: str, recipient_hex: str) -> str:
        network = Network.get_instance()
        proxy = None
        if network and network.proxy and network.proxy.enabled:
            proxy = make_aiohttp_proxy_connector(network.proxy, self._ssl)
        priv_hex = self.privkey_hex()
        priv = PrivateKey(bytes.fromhex(priv_hex))
        content = priv.encrypt_message(text, recipient_hex)
        log = self.logger.getChild('aionostr') if hasattr(self.logger, 'getChild') else None
        if log is not None:
            log.setLevel('INFO')
        async with aionostr.Manager(relays=self.relays(), private_key=priv_hex, ssl_context=self._ssl, proxy=proxy, log=log) as manager:
            eid = await asyncio.wait_for(
                aionostr._add_event(manager, kind=KIND_DM, content=content, private_key=priv_hex, tags=[['p', recipient_hex]]),
                timeout=SEND_TIMEOUT_S)
        return eid

    def send(self, text: str, *, recipient_hex: Optional[str] = None, on_done: Optional[Callable[[bool, str], None]] = None):
        """Fire and forget from any thread; `on_done(ok, info)` is called from the asyncio thread."""
        recipient_hex = recipient_hex or self.recipient_hex()
        if not recipient_hex:
            if on_done:
                on_done(False, 'no recipient')
            return
        try:
            loop = util.get_asyncio_loop()
        except Exception as e:
            if on_done:
                on_done(False, f'no event loop: {e}')
            return
        fut = asyncio.run_coroutine_threadsafe(self._send(text, recipient_hex), loop)

        def done(f):
            try:
                eid = f.result()
                self.logger.info(f'nostr DM sent: {eid}')
                if on_done:
                    on_done(True, str(eid))
            except Exception as e:
                self.logger.info(f'nostr DM failed: {e!r}')
                if on_done:
                    on_done(False, f'{type(e).__name__}: {e}')
        fut.add_done_callback(done)
        return fut
