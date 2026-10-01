"""Desktop notifications: Qt tray banner plus an in-app cue; optional osascript fallback on macOS."""
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow


def notify(window: 'ElectrumWindow', message: str, *, title: str = 'LowTide', use_osascript: bool = False) -> None:
    try:
        window.notify(message)
    except Exception:
        window.logger.exception('tray notification failed')
    if use_osascript and sys.platform == 'darwin':
        osascript_notify(title, message)


def osascript_notify(title: str, message: str) -> bool:
    """macOS fallback that needs no dependency. Returns True if the command ran."""
    def esc(s: str) -> str:
        return s.replace('\\', '\\\\').replace('"', '\\"')
    script = f'display notification "{esc(message)}" with title "{esc(title)}"'
    try:
        subprocess.Popen(['osascript', '-e', script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False
