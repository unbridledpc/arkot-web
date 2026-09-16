"""Login attempt limits, shared by the game client's login (`POST /api/login`)
and the website's login form (`POST /login`).

State lives in this process, behind one lock. That is correct only while
uvicorn runs a single worker (see Dockerfile); with more workers each would
keep its own counts, and the limits would have to move into the database.

Three limits, checked before any database work:

* per client IP, a token bucket: RATE attempts a minute, BURST at once;
* per (identifier, IP), FAILURES_PER_PAIR failures inside WINDOW lock that pair
  for LOCK seconds;
* per identifier from anywhere, FAILURES_PER_NAME failures inside WINDOW refuse
  further attempts until old failures age out. The threshold is deliberately
  high, so a stranger cannot cheaply lock the owner out of his own account.
"""
import collections
import ipaddress
import threading
import time

RATE_PER_MINUTE = 10
BURST = 5
WINDOW = 15 * 60
LOCK = 15 * 60
FAILURES_PER_PAIR = 5
FAILURES_PER_NAME = 30
MAX_KEYS = 50_000                 # memory bound per table; pruned past this

MESSAGE = "Too many login attempts. Please wait a few minutes."


def normalise(identifier: str) -> str:
    return (identifier or "").strip().lower()


class LoginLimiter:
    def __init__(self, rate_per_minute=RATE_PER_MINUTE, burst=BURST, window=WINDOW, lock=LOCK,
                 failures_per_pair=FAILURES_PER_PAIR, failures_per_name=FAILURES_PER_NAME,
                 clock=time.monotonic):
        self.rate = rate_per_minute / 60.0
        self.burst = float(burst)
        self.window = window
        self.lock_for = lock
        self.failures_per_pair = failures_per_pair
        self.failures_per_name = failures_per_name
        self.clock = clock
        self._lock = threading.Lock()
        self._buckets = {}                                        # ip -> [tokens, updated]
        self._pair_failures = collections.defaultdict(collections.deque)   # (name, ip) -> times
        self._pair_locked = {}                                    # (name, ip) -> until
        self._name_failures = collections.defaultdict(collections.deque)   # name -> times
        self._last_prune = 0.0

    def allow(self, ip: str, identifier: str) -> bool:
        """One login attempt. False means refuse it without looking anything up.
        An allowed attempt spends one of the IP's tokens."""
        now = self.clock()
        name = normalise(identifier)
        with self._lock:
            self._maybe_prune(now)
            until = self._pair_locked.get((name, ip))
            if until is not None:
                if now < until:
                    return False
                del self._pair_locked[(name, ip)]
            if name and self._recent(self._name_failures.get(name), now) >= self.failures_per_name:
                return False
            tokens, updated = self._buckets.get(ip, (self.burst, now))
            tokens = min(self.burst, tokens + (now - updated) * self.rate)
            if tokens < 1.0:
                self._buckets[ip] = [tokens, now]
                return False
            self._buckets[ip] = [tokens - 1.0, now]
            return True

    def failed(self, ip: str, identifier: str) -> None:
        now = self.clock()
        name = normalise(identifier)
        with self._lock:
            pair = self._pair_failures[(name, ip)]
            pair.append(now)
            if self._recent(pair, now) >= self.failures_per_pair:
                self._pair_locked[(name, ip)] = now + self.lock_for
                pair.clear()
            if name:
                self._name_failures[name].append(now)

    def succeeded(self, ip: str, identifier: str) -> None:
        """A correct password clears that pair's failures; the per-name count is
        left alone, so an attacker's own account cannot launder it."""
        with self._lock:
            self._pair_failures.pop((normalise(identifier), ip), None)

    def _recent(self, times, now) -> int:
        if not times:
            return 0
        while times and times[0] <= now - self.window:
            times.popleft()
        return len(times)

    def _maybe_prune(self, now) -> None:
        big = max(len(self._buckets), len(self._pair_failures), len(self._name_failures))
        if now - self._last_prune < 60 and big < MAX_KEYS:
            return
        self._last_prune = now
        for table in (self._pair_failures, self._name_failures):
            for key in [k for k, times in table.items() if not self._recent(times, now)]:
                del table[key]
        for key in [k for k, until in self._pair_locked.items() if until <= now]:
            del self._pair_locked[key]
        full_after = (self.burst / self.rate) if self.rate else 0
        for key in [k for k, (_, updated) in self._buckets.items() if now - updated >= full_after]:
            del self._buckets[key]        # refilled to BURST: same as never seen
        if len(self._buckets) >= MAX_KEYS:
            # Still flooded with distinct addresses: forget the stalest half. A
            # forgotten address starts again at BURST, which is the safe loss.
            stale = sorted(self._buckets, key=lambda k: self._buckets[k][1])
            for key in stale[:len(stale) // 2]:
                del self._buckets[key]


# Where a trusted reverse proxy can be: RFC 1918, loopback and IPv6 unique-local.
# Spelled out rather than ipaddress's is_private, which also covers documentation
# and reserved ranges that have nothing to do with our own network.
TRUSTED_PROXY_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7"))


def _trusted_proxy(addr) -> bool:
    return any(addr.version == net.version and addr in net for net in TRUSTED_PROXY_NETWORKS)


def _ip(text):
    try:
        addr = ipaddress.ip_address((text or "").strip())
    except ValueError:
        return None
    mapped = getattr(addr, "ipv4_mapped", None)
    return mapped or addr


def client_ip(peer: str, forwarded_for: str = "") -> str:
    """The address a request really came from.

    `X-Forwarded-For` is only believed when the direct peer is on a private or
    loopback network (TRUSTED_PROXY_NETWORKS): the reverse proxy on the Docker
    network. A request from anywhere else is taken at its word, header ignored,
    so nobody on the internet can pick the address they are rate-limited under. The right-most
    entry is used: it is the one our proxy wrote, while anything to its left
    came from the client.
    """
    direct = _ip(peer)
    if direct is None:
        return peer or ""
    if _trusted_proxy(direct) and forwarded_for:
        claimed = _ip(forwarded_for.split(",")[-1])
        if claimed is not None:
            return str(claimed)
    return str(direct)


def ipv4_int(text: str) -> int:
    """The address as account_sessions.ip stores it; 0 for anything not IPv4."""
    addr = _ip(text)
    return int(addr) if isinstance(addr, ipaddress.IPv4Address) else 0
