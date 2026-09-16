from app import ratelimit
from app.ratelimit import LoginLimiter


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def limiter(**kw):
    clock = Clock()
    return LoginLimiter(clock=clock, **kw), clock


def test_ip_burst_then_refill():
    lim, clock = limiter()
    assert all(lim.allow("1.2.3.4", f"user{i}") for i in range(ratelimit.BURST))
    assert not lim.allow("1.2.3.4", "another")
    assert lim.allow("5.6.7.8", "another")            # other addresses unaffected
    clock.now += 60 / ratelimit.RATE_PER_MINUTE        # one token back
    assert lim.allow("1.2.3.4", "x")
    assert not lim.allow("1.2.3.4", "x")


def test_ip_rate_sustained_is_ten_a_minute():
    lim, clock = limiter()
    allowed = 0
    for _ in range(600):                               # one attempt a second for ten minutes
        allowed += lim.allow("1.2.3.4", "x")
        clock.now += 1
    # BURST up front plus RATE per minute after that
    assert ratelimit.RATE_PER_MINUTE * 10 - 5 <= allowed <= ratelimit.RATE_PER_MINUTE * 10 + ratelimit.BURST


def test_pair_locks_after_five_failures_for_fifteen_minutes():
    lim, clock = limiter(rate_per_minute=10_000, burst=10_000)
    for _ in range(ratelimit.FAILURES_PER_PAIR - 1):
        assert lim.allow("1.1.1.1", "Owner")
        lim.failed("1.1.1.1", "Owner")
    assert lim.allow("1.1.1.1", "owner")               # 4 failures: still allowed
    lim.failed("1.1.1.1", " OWNER ")                   # identifier is normalised
    assert not lim.allow("1.1.1.1", "owner")
    assert lim.allow("2.2.2.2", "owner")               # the pair, not the name
    assert lim.allow("1.1.1.1", "someone-else")
    clock.now += ratelimit.LOCK - 1
    assert not lim.allow("1.1.1.1", "owner")
    clock.now += 2
    assert lim.allow("1.1.1.1", "owner")


def test_pair_failures_outside_window_do_not_count():
    lim, clock = limiter(rate_per_minute=10_000, burst=10_000)
    for _ in range(ratelimit.FAILURES_PER_PAIR - 1):
        lim.failed("1.1.1.1", "a")
    clock.now += ratelimit.WINDOW + 1
    lim.failed("1.1.1.1", "a")
    assert lim.allow("1.1.1.1", "a")


def test_success_clears_pair_failures():
    lim, _ = limiter(rate_per_minute=10_000, burst=10_000)
    for _ in range(ratelimit.FAILURES_PER_PAIR - 1):
        lim.failed("1.1.1.1", "a")
    lim.succeeded("1.1.1.1", "a")
    lim.failed("1.1.1.1", "a")
    assert lim.allow("1.1.1.1", "a")


def test_identifier_soft_lock_after_thirty_failures_from_many_addresses():
    lim, clock = limiter(rate_per_minute=10_000, burst=10_000)
    for i in range(ratelimit.FAILURES_PER_NAME - 1):
        lim.failed(f"10.0.{i}.1", "owner")
    assert lim.allow("9.9.9.9", "owner")
    lim.failed("10.0.99.1", "owner")
    assert not lim.allow("9.9.9.9", "owner")           # from anywhere
    assert lim.allow("9.9.9.9", "someone-else")
    clock.now += ratelimit.WINDOW + 1
    assert lim.allow("9.9.9.9", "owner")


def test_thresholds_are_the_planned_ones():
    assert (ratelimit.RATE_PER_MINUTE, ratelimit.BURST) == (10, 5)
    assert (ratelimit.FAILURES_PER_PAIR, ratelimit.FAILURES_PER_NAME) == (5, 30)
    assert ratelimit.WINDOW == ratelimit.LOCK == 15 * 60


def test_prune_keeps_limits():
    lim, clock = limiter(rate_per_minute=10_000, burst=10_000)
    for _ in range(ratelimit.FAILURES_PER_PAIR):
        lim.failed("1.1.1.1", "a")
    clock.now += 120                                   # triggers a prune on next allow
    assert not lim.allow("1.1.1.1", "a")


def test_client_ip_trusts_forwarded_only_from_private_peers():
    assert ratelimit.client_ip("172.18.0.5", "203.0.113.7") == "203.0.113.7"
    assert ratelimit.client_ip("127.0.0.1", "203.0.113.7") == "203.0.113.7"
    assert ratelimit.client_ip("172.18.0.5", "6.6.6.6, 203.0.113.7") == "203.0.113.7"
    assert ratelimit.client_ip("198.51.100.2", "203.0.113.7") == "198.51.100.2"
    assert ratelimit.client_ip("8.8.8.8", "10.0.0.1") == "8.8.8.8"
    assert ratelimit.client_ip("192.168.68.30", "8.8.4.4") == "8.8.4.4"
    assert ratelimit.client_ip("2001:4860::1", "8.8.4.4") == "2001:4860::1"
    assert ratelimit.client_ip("::1", "8.8.4.4") == "8.8.4.4"
    assert ratelimit.client_ip("172.18.0.5", "") == "172.18.0.5"
    assert ratelimit.client_ip("172.18.0.5", "garbage") == "172.18.0.5"
    assert ratelimit.client_ip("::ffff:172.18.0.5", "203.0.113.7") == "203.0.113.7"


def test_ipv4_int():
    assert ratelimit.ipv4_int("1.2.3.4") == 0x01020304
    assert ratelimit.ipv4_int("2001:db8::1") == 0
    assert ratelimit.ipv4_int("") == 0
