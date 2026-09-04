#!/usr/bin/env python3
"""Precompute the AMD registry's all-time boards from the Long factory's full history.

WHY THIS IS NOT DONE IN THE BROWSER
The live reservation set is one `eth_getLogs` (24h ~ 2.1k logs) and the page reads it directly.
COUNTING across all history is a different question: 45M+ blocks since 2026-07-13. Cheap as a batch
job (~60s, ~50 calls), impossible as a page load.

WHAT IS COUNTED
One `TokenCreated` event == one token contract == one transaction. Verified over the full history:
events == distinct token addresses == distinct transactions, and no ticker ever reuses a token
address. So "deployed 12x" means twelve separate contracts have launched under that ticker, each
holding it for its own 24h.

WHAT IS *NOT* COUNTED, and it matters
Long's web app also soft-holds a ticker while you fill in the launch form ("held for you for 24
hours"). That hold is OFF-CHAIN — typing a ticker writes nothing, costs no gas, and is invisible
here. Confirmed 2026-09-03: a ticker the app reported as held for the user read `filed=0 expires=0`
on chain and `isTickerAvailable` returned true. So these boards describe DEPLOYMENTS, never
intentions, and "available on-chain" does not imply "the app will let you have it".

FAIL LOUD. `rh_chain.get_logs` bisects on a range/size error and raises on anything else; a chunk
that silently returned nothing would undercount specific tickers rather than erroring, so coverage
is asserted before anything is written (D-RANK).
"""
import collections, json, os, sys, time, urllib.error, urllib.request

RPC = os.environ.get("RH_RPC", "https://rpc.mainnet.chain.robinhood.com")
UA = {"Content-Type": "application/json", "User-Agent": "amd-registry/1.0"}
MIN_INTERVAL = float(os.environ.get("RPC_MIN_INTERVAL", "0.2"))

# Deliberately standalone: this ships in the website repo and must run in CI with no checkout of the
# research repo and no pip install. The error handling mirrors that project's shared RPC helper, and
# the two rules it encodes are the ones that have actually cost data there:
#
#   * A JSON-RPC `error` arrives with HTTP 200. Reading `.get("result")` would turn it into None,
#     which then reads as "this range has no launches" -- a null about the REQUEST wearing the
#     costume of a fact about the world. So: raise.
#   * ...but this node fronts a pool of backends and reports THEIR transport failures inside that
#     same error object. Those are retryable blips; a range/size error is a real answer about the
#     request and must be BISECTED, never retried unchanged. Getting this backwards fails both ways
#     -- retrying a range error loops forever, raising on a blip throws away good work.
_TRANSIENT = ("connection refused", "eof", "dial tcp", "context deadline", "connection reset",
              "no such host", "i/o timeout", "bad gateway", "service unavailable",
              "internal server err", "try again", "temporarily unavailable", "too many requests")
_SPLITTY = ("more than", "limit exceeded", "log query timed out", "response size", "too large")
_last = [0.0]
_calls = [0]


class RpcError(RuntimeError):
    """A JSON-RPC error object. Distinct from a transport failure: it is an answer."""


def rpc(method, params, tries=7):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for attempt in range(tries):
        gap = MIN_INTERVAL - (time.time() - _last[0])
        if gap > 0:
            time.sleep(gap)
        _last[0] = time.time()
        _calls[0] += 1
        try:
            raw = urllib.request.urlopen(urllib.request.Request(RPC, body, UA), timeout=90).read()
        except (urllib.error.URLError, OSError):
            if attempt == tries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
            continue
        d = json.loads(raw)
        if "error" in d:
            msg = str(d["error"].get("message", d["error"]))
            if any(k in msg.lower() for k in _TRANSIENT) and attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RpcError(msg)
        return d["result"]
    raise RpcError("exhausted retries")


def get_logs(params, depth=0):
    """eth_getLogs, bisecting on the log cap / timeout rather than retrying it unchanged."""
    try:
        return rpc("eth_getLogs", [params])
    except RpcError as e:
        lo, hi = int(params["fromBlock"], 16), int(params["toBlock"], 16)
        if not any(k in str(e).lower() for k in _SPLITTY) or hi <= lo or depth > 26:
            raise
        mid = (lo + hi) // 2
        return (get_logs(dict(params, fromBlock=hex(lo), toBlock=hex(mid)), depth + 1)
                + get_logs(dict(params, fromBlock=hex(mid + 1), toBlock=hex(hi)), depth + 1))

FACTORY = "0x22e99278308b393ea1260859b181ad7e78f5eeed"
TOPIC0  = "0xadc6f1f726f7c710f77ec06adc75f3bb964e5be19581b072c67f7b9b4039267b"
GENESIS = 8_650_000                     # first creation event measured at ~8,656,459
CHUNK   = int(os.environ.get("CHUNK_BLOCKS", "1000000"))
ISSUER  = "https://api.robinhood.com/rhj/assets"
TOP_N   = 250
PAIR_N  = 60
DAYS    = 30                            # trailing days of launch volume for the sparkline


def stock_map():
    """Authoritative tokenised-stock contracts, straight from the issuer.

    Not the local CSV: that is a dated snapshot, and a stock token listed after it was taken would
    be silently misfiled as 'not a stock'. A3 -- this endpoint 403s without a User-Agent.
    """
    req = urllib.request.Request(ISSUER, headers={"User-Agent": "Mozilla/5.0"})
    data = json.loads(urllib.request.urlopen(req, timeout=30).read())
    items = data if isinstance(data, list) else (data.get("assets") or data.get("results") or [])
    out = {}
    for a in items:
        for dep in (a.get("deployments") or []):
            c = (dep.get("contractAddress") or "").lower()
            if c:
                out[c] = a.get("tokenSymbol") or a.get("symbol") or "?"
    if not out:
        raise RuntimeError("issuer returned no contracts -- refusing to classify every pair as non-stock")
    return out


def erc20_symbol(addr):
    try:
        h = rpc("eth_call", [{"to": addr, "data": "0x95d89b41"}, "latest"])[2:]
    except Exception:
        return None
    try:
        if len(h) >= 128:                                    # dynamic string
            ln = int(h[64:128], 16)
            return bytes.fromhex(h[128:128 + ln * 2]).decode("utf-8").strip() or None
        return bytes.fromhex(h).decode("utf-8").strip("\x00").strip() or None   # bytes32
    except Exception:
        return None


def decode(log):
    d = log["data"][2:]
    w = [d[i:i + 64] for i in range(0, len(d), 64)]
    off = int(w[5], 16) // 32
    ln = int(w[off], 16)
    raw = "".join(w[off + 1:])[:ln * 2]
    try:
        name = bytes.fromhex(raw).decode("utf-8")
    except Exception:
        return None
    if not (1 <= len(name) <= 15 and name.isascii() and name.isalpha()):
        return None
    return {"ticker": name.upper(), "quote": "0x" + log["topics"][3][-40:],
            "filed": int(w[3], 16), "expires": int(w[4], 16)}


def main():
    stocks = stock_map()
    print(f"issuer lists {len(stocks)} tokenised-stock contracts", file=sys.stderr)

    head = int(rpc("eth_blockNumber", []), 16)
    counts, first, last = {}, {}, {}
    pairs, pairs7 = collections.Counter(), collections.Counter()
    per_day = collections.Counter()
    events, covered, b = 0, 0, GENESIS
    now = int(time.time())
    t0 = time.time()
    while b <= head:
        hi = min(b + CHUNK, head)
        logs = get_logs({"fromBlock": hex(b), "toBlock": hex(hi),
                           "address": FACTORY, "topics": [TOPIC0]})
        for l in logs:
            r = decode(l)
            if not r:
                continue
            t = r["ticker"]
            counts[t] = counts.get(t, 0) + 1
            if t not in first or r["filed"] < first[t]: first[t] = r["filed"]
            if t not in last or r["filed"] > last[t]:   last[t] = r["filed"]
            pairs[r["quote"]] += 1
            if r["filed"] > now - 7 * 86400:
                pairs7[r["quote"]] += 1
            per_day[time.strftime("%Y-%m-%d", time.gmtime(r["filed"]))] += 1
            events += 1
        covered += hi - b + 1
        print(f"  {b:,}-{hi:,}  {len(logs):>5} logs  (total {events:,})", file=sys.stderr, flush=True)
        b = hi + 1

    want = head - GENESIS + 1
    assert covered == want, f"COVERAGE GAP: scanned {covered:,} of {want:,} blocks"

    # classify quote assets: a Long-deployed token's address ends 1e18, a stock is in the issuer list
    rows = []
    for addr, n in pairs.most_common(PAIR_N):
        if int(addr, 16) == 0:
            sym, cls = "ETH", "eth"
        elif addr in stocks:
            sym, cls = stocks[addr], "stock"
        else:
            sym = erc20_symbol(addr) or addr[:10] + "…"
            cls = "token" if addr.endswith("1e18") else "other"
        rows.append({"a": addr, "s": sym, "c": cls, "n": n, "w": pairs7.get(addr, 0)})

    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    # Every ticker deployed more than once, so the browser's delta-merge is EXACT for anything that
    # could ever reach a board. A ticker seen once cannot be "contested"; if the delta pushes it to
    # two it simply enters at the bottom, which no top-N view can show anyway.
    repeat = {t: n for t, n in counts.items() if n >= 2}

    out = {
        "generated_at": now, "from_block": GENESIS, "to_block": head, "blocks": want,
        "events": events, "distinct_tickers": len(counts),
        "pair_events": sum(pairs.values()), "pair_events_7d": sum(pairs7.values()),
        "top": [{"t": t, "n": n, "first": first[t], "last": last[t]} for t, n in top[:TOP_N]],
        "repeat": repeat,
        "per_day": [{"d": d, "n": n} for d, n in sorted(per_day.items())[-DAYS:]],
        "pairs": rows,
    }
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "counts.json")
    json.dump(out, open(p, "w"), separators=(",", ":"))

    print(f"\nscanned {want:,} blocks in {time.time()-t0:.0f}s, {_calls[0]} calls", file=sys.stderr)
    print(f"{events:,} deployments, {len(counts):,} distinct tickers, "
          f"{len(pairs):,} distinct quote assets -> {p} ({os.path.getsize(p):,} b)", file=sys.stderr)
    print("top tickers:", ", ".join(f"{t}x{n}" for t, n in top[:8]), file=sys.stderr)
    print("top pairs:  ", ", ".join(f"{r['s']}({r['c']}) {r['n']}" for r in rows[:8]), file=sys.stderr)
    print(f"repeat tickers (n>=2): {len(repeat):,}", file=sys.stderr)
    print("last 7 days:", ", ".join(f"{d['d'][5:]} {d['n']}" for d in out["per_day"][-7:]),
          file=sys.stderr)


if __name__ == "__main__":
    main()
