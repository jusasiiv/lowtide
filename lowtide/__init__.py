"""LowTide: send, consolidate and rescue at low tide.

Config variables are declared here so they exist before any GUI module loads.
"""
from electrum.simple_config import SimpleConfig, ConfigVar

plugin_name = "lowtide"

# Guard against the module being executed twice (internal + external load paths).
if not hasattr(SimpleConfig, 'LOWTIDE_MEMPOOL_URL'):
    SimpleConfig.LOWTIDE_MEMPOOL_URL = ConfigVar(
        key='plugins.lowtide.mempool_url', default='https://mempool.space', type_=str, plugin=plugin_name)
    SimpleConfig.LOWTIDE_NOTIFICATIONS = ConfigVar(
        key='plugins.lowtide.notifications', default=True, type_=bool, plugin=plugin_name)
    SimpleConfig.LOWTIDE_PRIVACY_MODE = ConfigVar(
        key='plugins.lowtide.privacy_mode', default=False, type_=bool, plugin=plugin_name)
    SimpleConfig.LOWTIDE_DEMO_MODE = ConfigVar(
        key='plugins.lowtide.demo_mode', default=False, type_=bool, plugin=plugin_name)
    SimpleConfig.LOWTIDE_POLL_MINUTES = ConfigVar(
        key='plugins.lowtide.poll_minutes', default=10, type_=int, plugin=plugin_name)
    SimpleConfig.LOWTIDE_OSASCRIPT_NOTIFY = ConfigVar(
        key='plugins.lowtide.osascript_notify', default=False, type_=bool, plugin=plugin_name)
    SimpleConfig.LOWTIDE_NOSTR_ENABLED = ConfigVar(
        key='plugins.lowtide.nostr_enabled', default=False, type_=bool, plugin=plugin_name)
    SimpleConfig.LOWTIDE_NOSTR_NPUB = ConfigVar(
        key='plugins.lowtide.nostr_npub', default='', type_=str, plugin=plugin_name)
    SimpleConfig.LOWTIDE_NOSTR_RELAYS = ConfigVar(
        key='plugins.lowtide.nostr_relays', default='', type_=str, plugin=plugin_name)
    SimpleConfig.LOWTIDE_NOSTR_PRIVKEY = ConfigVar(
        key='plugins.lowtide.nostr_privkey', default='', type_=str, plugin=plugin_name)
