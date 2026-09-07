#!/usr/bin/env python3
"""Rebuild ratings.json: what it costs to exit every liquid asset on Robinhood Chain.

WHAT THIS MEASURES
------------------
Not market cap, not TVL. The largest clip an asset absorbs before the exit cost of
selling it reaches 5%. Pool reserves are not depth: a pool's balance counts every price
range including the ones nowhere near spot, which is why assets with seven figures of
reserves show up here unable to clear a four-figure sell.

Impact is measured ORACLE-FREE, as a ratio of two quotes at different clip sizes:

    impact(N) = 1 - (out(N)/N) / (out(ref)/ref)

A constant proportional fee cancels between the legs, so no fee assumption is needed and
no USD oracle enters the arithmetic. Sizing still needs a price, and that price comes
from GeckoTerminal rather than from a quote we derived ourselves -- self-derived prices
were wrong for 69% of tokens under $1e-4, one by a factor of eleven million.

FIVE PASSES, all checkpointed to ratings_state.json, all fail-loud:

  discover  sample Transfer logs, decay-weight per-token activity        2 eth_getLogs
  classify  symbol, decimals, and supply authority for new addresses     eth_call/getCode
  price     GeckoTerminal, 30 tokens per request                         ~35 requests
  quote     the exit curve: 2 reference clips + 5 sizes, via KyberSwap   ~7 per asset
  probe     move a $1,000 clip and read what lands at the far end        1 eth_call each

WHAT IS DELIBERATELY NOT HERE
-----------------------------
Executing the aggregator's real router calldata through eth_simulateV1. It was built and
it worked -- it agreed with the quote on all 163 assets it reached, worst case 1.20% --
but this node meters eth_simulateV1 by execution cost rather than by request, so a full
pass took two and a half hours. The transfer probe is one eth_call per asset, covers the
whole book in minutes, and is the half that actually finds things.

RATE LIMITS, measured rather than assumed (2026-09-07):
  eth_call        ~5.5 req/s sustained
  eth_getLogs     roughly a dozen calls before a multi-minute lockout -- hence 2 per run
  Kyber           ~1.5 req/s sustained; answers "no route" with HTTP 400 code 4008,
                  which must NOT be read as a transport failure (see NO_ROUTE below)
  GeckoTerminal   ~1 req/2.2s
"""
import json, math, os, sys, time, urllib.request, urllib.error, collections, argparse, threading
from concurrent.futures import ThreadPoolExecutor

RPC = os.environ.get("RH_RPC", "https://rpc.mainnet.chain.robinhood.com")
KYBER = "https://aggregator-api.kyberswap.com/robinhood/api/v1"
GT = "https://api.geckoterminal.com/api/v2/networks/robinhood"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"      # 6 decimals, the chain's quote asset
PROBE = "0x00000000000000000000000000000000c1a1de00"
DEST = "0x00000000000000000000000000000000dead0001"
V4PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
TRANSFER_T0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Long's launchpad factory. topics[1] of TokenCreated is the token address, so watching
# this forward attributes every NEW Long launch for the cost of one eth_getLogs per run.
LONG_FACTORY = "0x22e99278308b393ea1260859b181ad7e78f5eeed"
LONG_T0 = "0xadc6f1f726f7c710f77ec06adc75f3bb964e5be19581b072c67f7b9b4039267b"

SIZES = [1_000, 10_000, 50_000, 250_000, 1_000_000]
REF = [30, 100]
BANDS = [(1_000_000, "AAA"), (500_000, "AA"), (250_000, "A"), (100_000, "BBB"),
         (50_000, "BB"), (25_000, "B"), (10_000, "CCC"), (0, "D")]
MAX_OTHERS = int(os.environ.get("MAX_OTHERS", "1000"))
DECAY = 0.85          # per run, so a token that stops trading falls out of the book
STATE = "ratings_state.json"
OUT = "ratings.json"

# Infrastructure, not a tradeable opinion.
SKIP = {USDG, "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
        "0xcec185eb182c47d1ba1efc84e6959e18cd620be4",
        "0x58daec3116aae6d93017baaea7749052e8a04fa7", V4PM}

# Kyber says "there is no route" with HTTP 400 + code 4008 rather than a 200 and an empty
# body. Reading that as a transport error is what silently dropped NVDA, MSFT, META,
# NFLX, LLY, MSTR, MU and LULU out of an earlier build of this page -- four of them AAA.
NO_ROUTE = ("route not found", "4008", "no route found")

SEL = {"symbol": "0x95d89b41", "name": "0x06fdde03", "decimals": "0x313ce567",
       "balanceOf": "0x70a08231", "implementation": "0x5c60da1b"}
MINT_SEL, BURN_SEL, PAUSE_SEL = "40c10f19", "9dc29fac", "8456cb59"

# The transfer probe's runtime, hand-assembled. Calldata is token|dest|amount; it calls
# transfer, then balanceOf(dest), and returns (transfer_ok, dest_balance). The transfer
# is a CALL, so a token that refuses returns 0 here instead of reverting the probe.
PROBE_CODE = ("63a9059cbb60e01b600052602035600452604035602452"
              "60206080604460006000600035" "5a" "f1" "60a052"
              "6370a0823160e01b600052602035600452"
              "602060c0602460006000" "35" "5a" "fa" "50" "604060a0f3")

STAT = collections.Counter()


class Throttled(RuntimeError):
    pass


# --------------------------------------------------------------------------- transport
def _http(url, body=None, headers=None, tries=6, timeout=60, label=""):
    h = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
    h.update(headers or {})
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, body, h)
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            txt = ""
            try:
                txt = e.read()[:300].decode("utf8", "replace")
            except Exception:
                pass
            if e.code == 400 and any(k in txt.lower() for k in NO_ROUTE):
                raise LookupError("no route")          # a real answer, not a failure
            last = f"HTTP {e.code} {txt[:140]}"
            if e.code in (429, 500, 502, 503, 504) and a < tries - 1:
                STAT["throttled"] += 1
                # eth_getLogs is budgeted at roughly a dozen calls before a lockout that
                # lasts minutes, so it gets a much longer wait than the other methods.
                # It is called twice per run; waiting is cheaper than losing discovery.
                slow = "getLogs" in (label or "")
                time.sleep(min(240.0 if slow else 30.0,
                               (20.0 if slow else 2.0) * (a + 1) ** 2))
                continue
            raise RuntimeError(f"{label}: {last}")
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if a < tries - 1:
                time.sleep(2.0 * (a + 1))
                continue
            raise RuntimeError(f"{label}: {last}")
    raise Throttled(f"{label}: {last}")


_rpc_last = [0.0]
_rpc_lock = threading.Lock()
RPC_GAP = float(os.environ.get("RPC_MIN_INTERVAL", "0.18"))
WORKERS = int(os.environ.get("WORKERS", "3"))


def rpc(method, params, tries=7):
    # The pacer is shared across workers, so it has to be locked -- otherwise three
    # threads each see a stale timestamp and the node gets three times the intended rate.
    with _rpc_lock:
        gap = RPC_GAP - (time.time() - _rpc_last[0])
        if gap > 0:
            time.sleep(gap)
        _rpc_last[0] = time.time()
    STAT["rpc"] += 1
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    d = _http(RPC, body, tries=tries, timeout=120, label=method)
    if "error" in d:
        raise RuntimeError(f"{method}: {str(d['error'])[:200]}")
    return d["result"]


def eth_call(to, data, overrides=None, tries=7):
    p = [{"to": to, "data": data}, "latest"]
    if overrides:
        p.append(overrides)
    return rpc("eth_call", p, tries=tries)


# ------------------------------------------------------------------------------ helpers
W = lambda a: a[2:].rjust(64, "0")
N = lambda n: "0x" + hex(n)[2:].rjust(64, "0")


def decode_string(hexstr):
    if not hexstr or len(hexstr) < 130:
        return None
    b = bytes.fromhex(hexstr[2:])
    try:
        off = int.from_bytes(b[:32], "big")
        ln = int.from_bytes(b[off:off + 32], "big")
        s = b[off + 32:off + 32 + ln].decode("utf8", "replace").strip()
    except Exception:
        return None
    s = "".join(ch for ch in s if ch.isprintable())[:40]
    return s or None


def depth_to(imp, thr=5.0):
    """Largest clip whose impact stays under thr%, log-interpolated between sampled sizes."""
    pts = [(s, v) for s, v in zip(SIZES, imp) if v is not None]
    if not pts:
        return None
    if pts[0][1] >= thr:
        return 0
    best = pts[0][0]
    for (s0, v0), (s1, v1) in zip(pts, pts[1:]):
        if v1 < thr:
            best = s1
            continue
        if v1 == v0:
            break
        f = (thr - v0) / (v1 - v0)
        return math.exp(math.log(s0) + f * (math.log(s1) - math.log(s0)))
    return best


def band(dep):
    if dep is None:
        return "NR"
    for lo, b in BANDS:
        if dep >= lo:
            return b
    return "D"


def load_state():
    if os.path.exists(STATE):
        st = json.load(open(STATE))
    else:
        st = {}
    for k in ("uni", "auth", "slot", "dec", "sym", "name", "plat"):
        st.setdefault(k, {})
    return st


def save_state(st):
    json.dump(st, open(STATE + ".partial", "w"))
    os.replace(STATE + ".partial", STATE)


# ----------------------------------------------------------------------- pass: discover
CAP_HINTS = ("exceeds limit", "more than", "limit exceeded", "response size", "too large")


def discover(st, windows=2):
    """Two eth_getLogs windows, adaptively sized.

    The node caps a query at 10,000 logs and REFUSES rather than truncating, so a fixed
    window breaks the moment the chain gets busier. Discovery only needs a sample, not a
    complete read, so the window shrinks until it fits and the working size is remembered
    -- which also keeps this to two getLogs calls per run, the budget that method allows.
    """
    head = int(rpc("eth_blockNumber", []), 16)
    span = int(st.get("span") or 4_000)
    seen = collections.Counter()
    for i in range(windows):
        hi = head - i * max(span, 1) * 3
        for _ in range(6):
            lo = hi - span + 1
            try:
                logs = rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi),
                                            "topics": [TRANSFER_T0]}], tries=3)
                break
            except Exception as e:
                if any(k in str(e).lower() for k in CAP_HINTS):
                    span = max(200, span // 2)
                    continue
                print(f"  window {i}: {str(e)[:110]}", file=sys.stderr, flush=True)
                logs = None
                break
        else:
            logs = None
        if logs is None:
            continue
        st["span"] = span
        for l in logs:
            seen[l["address"].lower()] += 1
        print(f"  window {i}: blocks {lo:,}-{hi:,} ({span:,})  {len(logs):,} logs  "
              f"{len(seen):,} tokens so far", flush=True)
    if not seen:
        # A cold state with no discovery is a blind rebuild -- refuse. A WARM state is a
        # different situation: the node was busy, and the previous universe is still the
        # best available answer. Do not decay on a failed pass, or repeated throttling
        # would quietly starve the book one run at a time.
        if not st["uni"]:
            raise RuntimeError("discovery returned no logs and the universe is empty "
                               "-- refusing to rebuild blind")
        print("  WARNING: discovery returned no logs; carrying the previous universe "
              f"({len(st['uni']):,} tokens) forward undecayed", file=sys.stderr, flush=True)
        STAT["discovery_failed"] += 1
        return head
    for a in list(st["uni"]):
        st["uni"][a]["act"] = round(st["uni"][a].get("act", 0.0) * DECAY, 4)
    now = int(time.time())
    for a, c in seen.items():
        if a in SKIP:
            continue
        e = st["uni"].setdefault(a, {"act": 0.0, "first": now})
        e["act"] = round(e.get("act", 0.0) + c, 4)
        e["seen"] = now
    # never let the universe grow without bound; keep what is alive plus every equity
    keep = {a for a, e in st["uni"].items() if e.get("act", 0) > 0.05 or e.get("kind") == "equity"}
    for a in list(st["uni"]):
        if a not in keep:
            del st["uni"][a]
    print(f"  universe: {len(st['uni']):,} tokens ({len(seen):,} active this run)", flush=True)
    return head


def scan_long(st, head):
    """Attribute new Long launches by watching the factory forward.

    The historical set came from a full-history factory scan and is carried in state for
    the tokens we already know. Back-filling seven million blocks of history on every cron
    is not affordable -- eth_getLogs is budgeted at roughly a dozen calls before a lockout
    -- so the cursor starts at the head it was seeded with and only moves forward. New
    launches are attributed the run after they happen; older tokens that were never in the
    universe stay unattributed, which the page reports as its own category rather than
    guessing.
    """
    lo = int(st.get("long_from") or (head - 300_000))
    if lo >= head:
        return 0
    # The chain makes ~213,000 blocks between 6-hourly runs, so a window smaller than
    # that never catches up. Factory logs are sparse (~47 per 20,000 blocks), so a wide
    # window is cheap; the adaptive shrink below covers a burst that trips the log cap.
    span = min(head - lo, 400_000)
    found = 0
    for _ in range(5):
        try:
            logs = rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(lo + span),
                                        "address": LONG_FACTORY, "topics": [LONG_T0]}], tries=3)
        except Exception as e:
            if any(k in str(e).lower() for k in CAP_HINTS):
                span = max(500, span // 2)
                continue
            print(f"  long factory: {str(e)[:110]}", file=sys.stderr, flush=True)
            return 0
        for l in logs:
            if len(l.get("topics", [])) > 1:
                tok = "0x" + l["topics"][1][-40:]
                st["plat"][tok.lower()] = "Long"
                found += 1
        st["long_from"] = lo + span + 1
        # Most launches never become liquid enough to rate, so the attribution map is
        # bounded: every universe member is kept, plus the most recent others (dicts
        # preserve insertion order, and JSON round-trips it) so a token that goes live
        # tomorrow is still attributable. Without this the map grows by ~900 entries a
        # run forever, in a file committed on every run.
        cap = 20_000
        if len(st["plat"]) > cap:
            uni = st["uni"]
            keep_uni = {a: v for a, v in st["plat"].items() if a in uni}
            rest = [(a, v) for a, v in st["plat"].items() if a not in uni]
            room = max(0, cap - len(keep_uni))
            keep_uni.update(dict(rest[-room:]))
            print(f"  plat pruned {len(st['plat']):,} -> {len(keep_uni):,}", flush=True)
            st["plat"] = keep_uni
        print(f"  long factory: blocks {lo:,}-{lo+span:,}  {found} launches attributed",
              flush=True)
        return found
    return 0


# ----------------------------------------------------------------------- pass: classify
def authority(addr):
    """mint / pause rights, read THROUGH beacon and clone indirection.

    Every tokenised equity on this chain is a 283-byte beacon-proxy stub whose real
    implementation is 11.6 KB and carries mint, burn and pause. Reading only the deployed
    bytecode scores all of them as fixed-supply tokens, which is wrong in the direction
    that flatters them.
    """
    code = rpc("eth_getCode", [addr, "latest"])[2:].lower()
    rec = {"n": len(code) // 2, "mint": MINT_SEL in code, "pause": PAUSE_SEL in code,
           "kind": "own"}
    if rec["n"] and rec["n"] < 400:
        impl = None
        i = code.find("363d3d373d3d3d363d73")
        if i >= 0:                                     # EIP-1167 minimal clone
            impl = "0x" + code[i + 20:i + 60]
            rec["kind"] = "clone"
        elif "5c60da1b" in code:                        # beacon proxy
            j = code.find("7f000000000000000000000000")
            if j >= 0:
                beacon = "0x" + code[j + 26:j + 66]
                try:
                    impl = "0x" + eth_call(beacon, SEL["implementation"])[-40:]
                    rec["kind"] = "beacon"
                    rec["beacon"] = beacon
                except Exception:
                    impl = None
        if impl:
            try:
                c = rpc("eth_getCode", [impl, "latest"])[2:].lower()
                rec.update({"via": impl, "n": len(c) // 2,
                            "mint": MINT_SEL in c, "pause": PAUSE_SEL in c})
            except Exception:
                pass
    return rec


def find_slot(addr):
    """Probe for the _balances slot. Never assume: Solady hashes a seed, not an index.

    Order matters for cost. Measured over 1,042 tokens: 64% sit at plain slot 0 and a
    further ~30% are Solady, so trying those two first takes the average from ~5 calls
    per token to under 2. The remaining plain indices are the long tail.
    """
    MAG = 7777 * 10 ** 18

    def hit(key):
        try:
            r = eth_call(addr, SEL["balanceOf"] + W(PROBE),
                         {addr: {"stateDiff": {key: N(MAG)}}}, tries=3)
        except Exception:
            return False
        return len(r) >= 66 and int(r, 16) == MAG

    if hit(keccak_map(PROBE, 0)):
        return ["plain", 0]
    sk = solady_key(PROBE)
    if hit(sk):
        return ["solady", sk]
    for s in range(1, 8):
        if hit(keccak_map(PROBE, s)):
            return ["plain", s]
    return None


def classify(st, addrs):
    todo = [a for a in addrs if a not in st["dec"]]
    if todo:
        print(f"  classifying {len(todo):,} new addresses", flush=True)
    for i, a in enumerate(todo):
        try:
            st["dec"][a] = int(eth_call(a, SEL["decimals"], tries=3), 16)
        except Exception:
            st["dec"][a] = None
        try:
            st["sym"][a] = decode_string(eth_call(a, SEL["symbol"], tries=3)) or a[:8]
        except Exception:
            st["sym"][a] = a[:8]
        try:
            st["name"][a] = decode_string(eth_call(a, SEL["name"], tries=3)) or ""
        except Exception:
            st["name"][a] = ""
        try:
            st["auth"][a] = authority(a)
        except Exception:
            st["auth"][a] = {}
        if st["auth"].get(a, {}).get("kind") == "beacon":
            st["uni"].setdefault(a, {})["kind"] = "equity"
        if st["dec"][a] is not None:
            try:
                st["slot"][a] = find_slot(a)
            except Exception:
                st["slot"][a] = None
        if i and i % 100 == 0:
            save_state(st)
            print(f"    ..{i}/{len(todo)}", flush=True)
    save_state(st)


# -------------------------------------------------------------------------- pass: price
def fetch_prices(addrs):
    px, res, nm = {}, {}, {}
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i + 30]
        try:
            d = _http(f"{GT}/tokens/multi/{','.join(chunk)}", tries=4,
                      timeout=45, label="geckoterminal")
        except Exception as e:
            print(f"  price chunk {i}: {str(e)[:110]}", file=sys.stderr, flush=True)
            time.sleep(3)
            continue
        for t in d.get("data", []):
            a = (t.get("attributes", {}).get("address") or "").lower()
            at = t.get("attributes", {})
            try:
                p = float(at.get("price_usd")) if at.get("price_usd") else None
            except (TypeError, ValueError):
                p = None
            if a and p and p > 0:
                px[a] = p
            if a and at.get("total_reserve_in_usd"):
                try:
                    res[a] = float(at["total_reserve_in_usd"])
                except (TypeError, ValueError):
                    pass
            if a and at.get("name"):
                nm[a] = at["name"]
        time.sleep(2.2)
    return px, res, nm


# -------------------------------------------------------------------------- pass: quote
def kyber_out(token, amount):
    """-> int amountOut, or None when the aggregator says there is genuinely no route."""
    try:
        d = _http(f"{KYBER}/routes?tokenIn={token}&tokenOut={USDG}"
                  f"&amountIn={amount}&gasInclude=false", tries=5, timeout=45, label="kyber")
    except LookupError:
        STAT["no_route"] += 1
        return None
    if d.get("code") != 0 or not (d.get("data") or {}).get("routeSummary"):
        STAT["no_route"] += 1
        return None
    STAT["kyber"] += 1
    return int(d["data"]["routeSummary"]["amountOut"])


def curve(token, px, dec):
    """The exit curve. Returns (imp[5], note)."""
    unit = 10 ** dec
    px0 = 0.0
    for c in REF:
        a = max(1, int(unit * c / px))
        out = kyber_out(token, a)
        if out:
            px0 = max(px0, out / a)
    if px0 <= 0:
        return [None] * 5, "no market at reference size"
    imp, dead = [], False
    for usd in SIZES:
        if dead:
            imp.append(None)
            continue
        a = max(1, int(unit * usd / px))
        out = kyber_out(token, a)
        if not out:
            imp.append(None)
            dead = True
            continue
        pc = round((1 - (out / a) / px0) * 100, 2)
        imp.append(pc)
        if pc >= 95.0:
            dead = True
    if imp[0] is None:
        return [None] * 5, "no fill at $1,000"
    v = [x for x in imp if x is not None]
    if v and min(v) < -1.0:
        return [None] * 5, "reference unreliable"
    return imp, None


# -------------------------------------------------------------------------- pass: probe
def keccak_map(key_hex, slot):
    from keccak import keccak256
    k = bytes.fromhex(key_hex[2:].rjust(64, "0"))
    return "0x" + keccak256(k.rjust(32, b"\0") + slot.to_bytes(32, "big")).hex()


def solady_key(owner):
    from keccak import keccak256
    return "0x" + keccak256(bytes.fromhex(owner[2:]) + b"\x00" * 8
                            + (0x87a211a2).to_bytes(4, "big")).hex()


def probe_transfer(token, slot, dec, px):
    """Move a $1,000 clip and read what lands. -> dict."""
    amt = max(1, int(1000.0 / px * (10 ** dec)))
    data = "0x" + W(token) + W(DEST) + hex(amt)[2:].rjust(64, "0")
    if slot:
        key = slot[1] if slot[0] == "solady" else keccak_map(PROBE, slot[1])
        ov = {PROBE: {"code": "0x" + PROBE_CODE}, token: {"stateDiff": {key: N(amt)}}}
        r = eth_call(PROBE, data, ov, tries=4)
        how = "slot"
    else:
        held = int(eth_call(token, SEL["balanceOf"] + W(V4PM), tries=3), 16)
        if held < amt:
            return {"v": "u", "why": "no balance slot and no donor holds the clip"}
        r = eth_call(V4PM, data, {V4PM: {"code": "0x" + PROBE_CODE}}, tries=4)
        how = "donor"
    ok, got = int(r[2:66], 16), int(r[66:130], 16)
    if not ok:
        # A refusal seen ONLY when pulling out of the v4 singleton is a hook settlement
        # rule, not a blocked token -- the probe is by construction the unauthorised
        # caller there. Only a directly-funded refusal counts.
        return ({"v": "r", "how": how} if how == "slot"
                else {"v": "u", "why": "refused only from the v4 singleton"})
    loss = (amt - got) / amt * 100.0
    return {"v": ("s" if loss > 0.5 else "t"), "how": how, "loss": round(loss, 3)}


# --------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="cap the scan list (for testing)")
    ap.add_argument("--skip-discover", action="store_true")
    a = ap.parse_args()

    t0 = time.time()
    st = load_state()
    print("discover", flush=True)
    head = int(rpc("eth_blockNumber", []), 16) if a.skip_discover else discover(st)
    save_state(st)

    try:
        scan_long(st, head)
    except Exception as e:
        print(f"  long factory scan skipped: {str(e)[:110]}", file=sys.stderr, flush=True)
    save_state(st)

    equities = [x for x, e in st["uni"].items() if e.get("kind") == "equity"]
    others = sorted([x for x, e in st["uni"].items() if e.get("kind") != "equity"],
                    key=lambda x: -st["uni"][x].get("act", 0))[:MAX_OTHERS]
    scan = equities + others
    if a.limit:
        scan = scan[:a.limit]
    print(f"scan list: {len(scan):,} ({len(equities):,} equities + {len(others):,} other)",
          flush=True)

    print("classify", flush=True)
    classify(st, scan)

    print("price", flush=True)
    px, res, gtname = fetch_prices(scan)
    print(f"  priced {len(px):,}/{len(scan):,}", flush=True)

    print(f"quote + probe ({WORKERS} workers)", flush=True)
    rows = []
    prog = {"done": 0}
    plock = threading.Lock()

    def one(addr):
        dec, p = st["dec"].get(addr), px.get(addr)
        if dec is None or not p:
            return None
        try:
            imp, note = curve(addr, p, dec)
        except Exception as e:
            print(f"  {addr} curve: {str(e)[:100]}", file=sys.stderr, flush=True)
            return None
        if imp[0] is None:
            return None
        try:
            pr = probe_transfer(addr, st["slot"].get(addr), dec, p)
        except Exception as e:
            pr = {"v": "u", "why": type(e).__name__}
        auth = st["auth"].get(addr) or {}
        is_eq = st["uni"].get(addr, {}).get("kind") == "equity"
        dep = depth_to(imp)
        row = {"s": st["sym"].get(addr) or addr[:8],
               "nm": st["name"].get(addr) or gtname.get(addr) or "",
               "a": addr[:6] + "…" + addr[-4:], "f": addr,
               "k": "e" if is_eq else "o",
               "b": band(dep), "d": (None if dep is None else int(round(dep))),
               "i": imp, "r": (int(res[addr]) if addr in res else None),
               "p": ("Robinhood" if is_eq else (st["plat"].get(addr) or "")),
               "g": pr["v"]}
        if auth.get("mint"):
            row["m"] = 2 if is_eq else 1
        if auth.get("pause"):
            row["pz"] = 1
        return row

    def work(addr):
        r = one(addr)
        with plock:
            prog["done"] += 1
            if r:
                rows.append(r)
            if prog["done"] % (25 if len(scan) > 200 else 5) == 0:
                el = time.time() - t0
                rate = prog["done"] / max(el, 1)
                eta = (len(scan) - prog["done"]) / max(rate, 1e-6)
                print(f"  ..{prog['done']}/{len(scan)}  rated {len(rows)}  "
                      f"{el:.0f}s elapsed, ~{eta/60:.0f} min left  {dict(STAT)}", flush=True)
        return r

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(work, scan))

    save_state(st)
    rows.sort(key=lambda r: (-(r["d"] or 0), r["s"].upper()))
    out = {"sizes": SIZES, "at": int(time.time()), "to_block": head, "rows": rows,
           "screen": {
               "transfers": sum(1 for r in rows if r["g"] == "t"),
               "taxed": sum(1 for r in rows if r["g"] == "s"),
               "restricted": sum(1 for r in rows if r["g"] == "r"),
               "unmeasured": sum(1 for r in rows if r["g"] == "u"),
               "open_supply": sum(1 for r in rows if r.get("m") == 1),
               "issuer_supply": sum(1 for r in rows if r.get("m") == 2),
               "pausable": sum(1 for r in rows if r.get("pz")),
           }}
    json.dump(out, open(OUT + ".partial", "w"), separators=(",", ":"))
    os.replace(OUT + ".partial", OUT)
    print(f"\nwrote {OUT}: {len(rows):,} rated assets in {time.time()-t0:.0f}s  {dict(STAT)}",
          flush=True)
    print("  " + json.dumps(out["screen"]), flush=True)


if __name__ == "__main__":
    main()
