"""LowTideService: one daemon thread that fetches, records, computes and publishes.

No Qt in here. HTTP is injected as a callable so the core stays testable; in Electrum the
callable goes through Network.send_http_on_proxy (proxy/Tor aware).
"""
import importlib
import json
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Sequence

H = importlib.import_module('.core_histogram', __package__)
E = importlib.import_module('.core_eta', __package__)
HI = importlib.import_module('.core_history', __package__)
F = importlib.import_module('.core_forecast', __package__)
B = importlib.import_module('.core_backtest', __package__)
M = importlib.import_module('.core_mempool_api', __package__)
from .core_store import Store

HttpGet = Callable[[str, float], Any]   # (url, timeout_s) -> parsed JSON; raises on failure


class ServiceError(Exception):
    pass


class State:
    """Snapshot of everything the UI needs. Replaced atomically on each update."""

    def __init__(self):
        self.hist = []            # type: H.Hist
        self.hist_source = ''     # 'mempool' | 'server' | 'cache'
        self.mempool_blocks = []  # type: List[dict]
        self.blocks = []          # type: List[dict]
        self.recommended = {}     # type: Dict[str, float]
        self.prices = {}          # type: Dict[str, float]
        self.forecast = None      # type: Optional[F.Forecast]
        self.past_week = []       # type: List[HI.HistoryPoint]
        self.data_as_of = 0
        self.history_as_of = 0
        self.fetched_at = 0
        self.last_error = ''
        self.offline = False
        self.backtest = {}        # type: dict
        self.spikes = {}          # type: Dict[int, float]
        self.next_block_rate = None  # type: Optional[float]
        self.pile = None          # type: Optional[H.PileJump]
        self.mempool_min_fee = H.FLOOR_RATE
        self.arrival_fn = lambda rate: 0.0  # type: E.ArrivalFn
        self.demo = {}            # type: Dict[str, Any]
        self.computed_at = 0

    # --- derived helpers used by the UI ------------------------------

    def now(self) -> int:
        return int(time.time())

    def tide_fn(self):
        fc = self.forecast
        if fc is None:
            return None
        now = self.now()
        return lambda rate: fc.time_until_rate_clears(rate, now)

    def eta(self, rate: float) -> Optional[E.Eta]:
        if not self.hist:
            return None
        return E.eta_for_rate(self.hist, rate, self.arrival_fn, tide_fn=self.tide_fn())

    def rate_for_deadline(self, seconds: float, *, max_rate: Optional[float] = None) -> Optional[E.Eta]:
        if not self.hist:
            return None
        return E.rate_for_deadline(self.hist, seconds, self.arrival_fn, max_rate=max_rate, tide_fn=self.tide_fn())

    def best_send_time(self, deadline_ts: int, *, margin_s: float = 3 * 3600) -> Optional[F.HourPoint]:
        """Cheapest forecast hour that still leaves `margin_s` before the deadline."""
        fc = self.forecast
        if fc is None:
            return None
        cands = [p for p in fc.horizon if p.ts + 3600 <= deadline_ts - margin_s and p.ts + 3600 > self.now()]
        if not cands:
            return None
        return min(cands, key=lambda p: (p.median, p.ts))

    def current_window(self) -> Optional[F.Window]:
        if self.demo.get('low_tide_now') and self.forecast:
            now = self.now()
            return F.Window(start=(now // 3600) * 3600, end=(now // 3600) * 3600 + 4 * 3600,
                            rate=self.pile.rate if self.pile else 0.5)
        return self.forecast.current_window(self.now()) if self.forecast else None

    def next_window(self) -> Optional[F.Window]:
        return self.forecast.next_window(self.now()) if self.forecast else None


class LowTideService(threading.Thread):

    LIVE_TIMEOUT = 20.0
    HISTORY_TIMEOUT = 60.0

    def __init__(self, *, store: Store, http_get: HttpGet, base_url_fn: Callable[[], str],
                 poll_minutes_fn: Callable[[], int], privacy_mode_fn: Callable[[], bool],
                 server_histogram_fn: Callable[[], Optional[H.Hist]], logger=None):
        threading.Thread.__init__(self, name='LowTideService', daemon=True)
        self.store = store
        self.http_get = http_get
        self.base_url_fn = base_url_fn
        self.poll_minutes_fn = poll_minutes_fn
        self.privacy_mode_fn = privacy_mode_fn
        self.server_histogram_fn = server_histogram_fn
        self.logger = logger
        self.state = State()
        self._listeners = []  # type: List[Callable[[State], None]]
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_live = 0.0
        self._last_hist_1w = 0.0
        self._last_hist_long = 0.0
        self._last_backtest = 0.0
        self._hourly = []  # type: List[HI.HistoryPoint]
        self._history = {}  # type: Dict[str, Any]
        self._backoff = 0.0
        self._fails = 0
        self.load_from_disk()

    # --- lifecycle ----------------------------------------------------

    def add_listener(self, fn: Callable[[State], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        try:
            self._listeners.remove(fn)
        except ValueError:
            pass

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def refresh_now(self) -> None:
        self._last_live = 0.0
        self._backoff = 0.0
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                self._log('tick failed: ' + traceback.format_exc())
            self._wake.wait(30.0)
            self._wake.clear()

    def _log(self, msg: str) -> None:
        if self.logger:
            self.logger.info(msg)

    def _publish(self) -> None:
        st = self.state
        for fn in list(self._listeners):
            try:
                fn(st)
            except Exception:
                self._log('listener failed: ' + traceback.format_exc())

    # --- scheduling ---------------------------------------------------

    def tick(self) -> None:
        now = time.time()
        changed = False
        poll = max(5, int(self.poll_minutes_fn() or 10)) * 60
        if now - self._last_live >= poll and now >= self._backoff:
            changed |= self._refresh_live()
        if now - self._last_hist_1w >= 3600 and now >= self._backoff:
            changed |= self._refresh_history(long=(now - self._last_hist_long >= 86400))
        if changed or not self.state.computed_at:
            self._compute()
            self._publish()

    # --- fetching -----------------------------------------------------

    def _get(self, name: str, timeout: float, **kw) -> Any:
        url = M.url(self.base_url_fn(), name, **kw)
        return self.http_get(url, timeout)

    def _refresh_live(self) -> bool:
        st = self.state
        try:
            if self.privacy_mode_fn():
                hist = self.server_histogram_fn() or []
                if not hist:
                    raise ServiceError('no server histogram yet')
                st.hist, st.hist_source = H.normalize(hist), 'server'
                st.mempool_blocks, st.blocks = [], []
            else:
                st.hist, st.hist_source = M.parse_histogram(self._get('mempool', self.LIVE_TIMEOUT)), 'mempool'
                st.mempool_blocks = M.parse_mempool_blocks(self._get('mempool_blocks', self.LIVE_TIMEOUT))
                st.blocks = M.parse_blocks(self._get('blocks', self.LIVE_TIMEOUT))
                try:
                    st.recommended = M.parse_recommended(self._get('recommended', self.LIVE_TIMEOUT))
                except Exception:
                    pass
                if time.time() - self._history.get('prices_at', 0) > 3600:
                    try:
                        st.prices = self._get('prices', self.LIVE_TIMEOUT)
                        self._history['prices_at'] = time.time()
                    except Exception:
                        pass
            st.data_as_of = st.fetched_at = int(time.time())
            st.offline, st.last_error = False, ''
            self._last_live = time.time()
            self._backoff = 0.0
            self._fails = 0
            self.store.append_snapshot(hist=st.hist, blocks=st.blocks, next_block_rate=H.next_block_rate(st.hist),
                                       extra={'source': st.hist_source})
            self._log(f'live refresh ok ({st.hist_source}): next block {H.next_block_rate(st.hist)} sat/vB, {len(st.hist)} histogram entries')
            self._save_cache()
            return True
        except Exception as e:
            err = f'{type(e).__name__}: {e}'[:200]
            if err != st.last_error:
                self._log(f'live refresh failed: {err}')
            st.last_error = err
            st.offline = True
            self._fails += 1
            self._backoff = time.time() + min(900, 60 * (2 ** min(self._fails, 4)))
            if not st.hist:
                hist = self.server_histogram_fn() or []
                if hist:
                    st.hist, st.hist_source = H.normalize(hist), 'server'
                    return True
            return False

    def _refresh_history(self, *, long: bool) -> bool:
        try:
            self._history['statistics_1w'] = self._get('statistics', self.HISTORY_TIMEOUT, period='1w')
            self._last_hist_1w = time.time()
            if long or 'statistics_3m' not in self._history:
                self._history['statistics_1m'] = self._get('statistics', self.HISTORY_TIMEOUT, period='1m')
                self._history['statistics_3m'] = self._get('statistics', self.HISTORY_TIMEOUT, period='3m')
                try:
                    self._history['feerates_3y'] = self._get('feerates', self.HISTORY_TIMEOUT, period='3y')
                    self._history['feerates_all'] = self._get('feerates', self.HISTORY_TIMEOUT, period='all')
                except Exception:
                    pass
                self._last_hist_long = time.time()
            self._history['as_of'] = int(time.time())
            self.store.save_history(self._history)
            self._log(f'history refresh ok (long={long})')
            return True
        except Exception as e:
            self._log(f'history refresh failed: {type(e).__name__}: {e}')
            self._backoff = time.time() + 300
            return False

    # --- computing ----------------------------------------------------

    def _compute(self) -> None:
        st = self.state
        now = int(time.time())
        if st.hist:
            st.next_block_rate = H.next_block_rate(st.hist)
            st.pile = H.pile_jump(st.hist)
            st.mempool_min_fee = H.min_fee(st.hist)
            snaps = self.store.last_snapshots(2)
            delta = None
            if len(snaps) == 2 and snaps[1]['ts'] - snaps[0]['ts'] >= 300:
                delta = (self.store.expand_compact(snaps[0]['hist']), self.store.expand_compact(snaps[1]['hist']),
                         float(snaps[1]['ts'] - snaps[0]['ts']))
            st.arrival_fn = E.make_arrival_fn(st.blocks, delta)
        series = []
        for key in ('statistics_3m', 'statistics_1m', 'statistics_1w'):
            if self._history.get(key):
                series.append(M.parse_statistics(self._history[key]))
        if series:
            sub1 = HI.sub1_resolver_from_hist(st.hist) if st.hist else None
            self._hourly = HI.hourly(HI.merge_series(*series, sub1=sub1))
            st.history_as_of = self._history.get('as_of', 0)
        if self._hourly:
            live = st.next_block_rate if (st.hist and now - st.data_as_of < 3600) else None
            try:
                st.forecast = F.build_forecast(self._hourly, now, live, data_as_of=max(st.data_as_of, st.history_as_of))
            except Exception as e:
                self._log(f'forecast failed: {e}')
            st.past_week = [p for p in self._hourly if p.ts >= now - 7 * 86400]
            if now - self._last_backtest > 86400 or not st.backtest:
                try:
                    st.backtest = B.run_backtest(self._hourly)
                    st.backtest['headline'] = B.headline(st.backtest)
                    self._last_backtest = now
                except Exception as e:
                    self._log(f'backtest failed: {e}')
        spikes = {}
        for key in ('feerates_all', 'feerates_3y'):
            if self._history.get(key):
                spikes.update(M.spike_levels(self._history[key]))
        st.spikes = {y: spikes.get(y, M.SPIKE_FALLBACK[y]) for y in M.SPIKE_FALLBACK}
        st.computed_at = now
        self._save_cache()

    # --- persistence --------------------------------------------------

    def _save_cache(self) -> None:
        st = self.state
        try:
            self.store.save_cache({
                'hist': st.hist, 'hist_source': st.hist_source, 'mempool_blocks': st.mempool_blocks,
                'blocks': st.blocks, 'recommended': st.recommended, 'prices': st.prices,
                'data_as_of': st.data_as_of, 'fetched_at': st.fetched_at,
                'forecast': st.forecast.to_dict() if st.forecast else None,
                'backtest': st.backtest, 'spikes': st.spikes,
                'last_backtest': self._last_backtest,
            })
        except Exception as e:
            self._log(f'cache save failed: {e}')

    def load_from_disk(self) -> None:
        st = self.state
        c = self.store.load_cache()
        if c:
            st.hist = H.normalize(c.get('hist') or [])
            st.hist_source = 'cache'
            st.mempool_blocks = c.get('mempool_blocks') or []
            st.blocks = c.get('blocks') or []
            st.recommended = c.get('recommended') or {}
            st.prices = c.get('prices') or {}
            st.data_as_of = c.get('data_as_of') or 0
            st.backtest = c.get('backtest') or {}
            st.spikes = {int(k): v for k, v in (c.get('spikes') or {}).items()}
            self._last_backtest = c.get('last_backtest') or 0
            if c.get('forecast'):
                try:
                    st.forecast = F.Forecast.from_dict(c['forecast'])
                except Exception:
                    st.forecast = None
            st.offline = True
        self._history = self.store.load_history() or {}
        if self._history.get('as_of'):
            self._last_hist_1w = self._history['as_of']
            self._last_hist_long = self._history['as_of'] if self._history.get('statistics_3m') else 0
        if st.hist or self._history:
            try:
                self._compute()
            except Exception:
                self._log('compute from cache failed: ' + traceback.format_exc())
