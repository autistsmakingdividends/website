# autistsmakingdividends.com

Static site. **No backend, no database, no API keys.** Two pages plus two images — drag the folder
onto Netlify / Cloudflare Pages / Vercel / GitHub Pages and it is live.

| File | What it is |
|---|---|
| `index.html` | Homepage — incorporation notice, manifesto, business model, Q3 results, internal memo, org chart, departments, approved terminology, the filing. |
| `registry.html` | **Office of Ticker Reservations** — the ticker-expiry tool. |
| `logo-96.png`, `logo-256.png` | The mark, cropped from `../amd.png` and downsampled. |
| `amd-intro.mp4` | 26s corporate film, 960x960 H.264/AAC, 19.8 MB. Faststart, so it streams. `preload="none"` and no poster: nothing is fetched until someone presses play. |
| `counts.json` | All-time boards: per-ticker deploy counts, quote-asset pairings, daily volume. |
| `build_counts.py` | Full-history scan that writes `counts.json`. Stdlib-only, runs in CI. |
| `../.github/workflows/counts.yml` | Rebuilds `counts.json` every 3h and commits it. |

## Deploying

This directory **is** the website root. It is a plain static site — no build step, no server, no
env vars — so hosting it is a one-time setup.

### GitHub Pages (recommended)

Fewest moving parts: the counts workflow already commits `counts.json` to this repo, and Pages
redeploys on every push, so scheduled refreshes go live with no second service involved.

```sh
cd AMD/site
git init -b main
git add -A && git commit -m "AMD registry site"
gh repo create amd-site --public --source=. --push      # or create it on github.com and push
```

Then in the repo: **Settings → Pages → Source: Deploy from a branch → `main` / `/ (root)`**.

The site serves at **https://autistsmakingdividends.com** (attached 2026-09-04), with
`autistsmakingdividends.github.io/website` redirecting to it.

DNS lives at Namecheap. The records, for reference — and the order matters: **point DNS first, add
the `CNAME` file second.** A `CNAME` naming a domain that does not resolve makes Pages 301 every
`github.io` URL to it, so the site deploys fine and is reachable by nobody:

| Type | Name | Value |
|---|---|---|
| A | `@` | `185.199.108.153` |
| A | `@` | `185.199.109.153` |
| A | `@` | `185.199.110.153` |
| A | `@` | `185.199.111.153` |
| CNAME | `www` | `<your-github-username>.github.io` |

The repo must be **public** for Pages on a free account. Nothing here is secret — there are no keys,
and the RPC it reads is public and keyless.

### Cloudflare Pages (if it goes viral)

Same effort — connect the repo, set build command to none and output directory to `/`. Worth it
only for the **unlimited bandwidth**: GitHub Pages has a 100 GB/month soft limit, which at ~140 KB
per visit is roughly 700k pageviews before anyone emails you. Cloudflare also gives you DDoS
protection and one-click DNS if the domain is registered there.

### Netlify Drop (fastest, but don't)

Dragging this folder onto `netlify.com/drop` publishes it in seconds with no git at all. Avoid it
as the real host: with no repo there is nothing for the counts workflow to push to, so the all-time
boards freeze at whatever `counts.json` you dragged. Fine for showing someone; wrong for launch.

## Why there is no backend

The ticker hold is enforced **on-chain** by the Long factory
(`0x22e99278308b393ea1260859b181ad7e78f5eeed`), and the Robinhood Chain RPC serves
`access-control-allow-origin: *`. So the browser reads the contract directly:

| Call | Selector | Returns |
|---|---|---|
| reservation record | `0xe07511a0(string)` | `(address token, uint256 filed, uint256 expires)` |
| availability | `0x22d38a76` `isTickerAvailable(string)` | `bool` |
| hold length | `0x8f27bbc4` `RESERVATION_DURATION()` | `86400` |

A lookup is **one batched request of two `eth_call`s**. The page asks both and reports it plainly if
they ever disagree, rather than inferring availability from the expiry alone — if Long later adds a
blocklist, the tool says "withheld for a reason it does not publish" instead of lying.

The "recently filed" tape is **one** `eth_getLogs` over ~1h of blocks, cached in `localStorage` for
90s so a busy page is not a burden on a public node.

Full derivation, traps, and the `long_tickers.py` CLI:
[`../../rh-platform/research/data-sources.md`](../../rh-platform/research/data-sources.md).

## The four boards

**Expiring next** is live: **one** `eth_getLogs` over ~900k blocks (~25.4h) returns every
reservation in the window — 2,119 logs, 2.4 MB of JSON, **~412 KB on the wire** after gzip, in under
2s. It is deduped to the latest expiry per ticker and sorted ascending. Cached 3 min in
`localStorage`. The same call feeds the scrolling tape, so the page makes one request, not two.

**Most contested**, **Contested & free** and **Most paired** cannot be done in the browser —
counting how often a ticker has *ever* been deployed needs the factory's whole history. Measured:
**45.1M blocks (since 2026-07-13) in ~60 seconds, 55 RPC calls → 19,231 deployments across 12,477
distinct tickers.** Cheap, but a batch job. `build_counts.py` writes `counts.json` (50 KB, 18 KB
gzipped) and the page fetches it.

## Keeping it fresh — three layers

A precomputed file starts ageing the moment it is written, so freshness is not left to trust:

1. **`.github/workflows/counts.yml`** rebuilds `counts.json` every 3 hours and commits it, which
   redeploys the site. The workflow **verifies the artefact before committing** — a scan that
   returns junk exits 0 just as happily as one that works, so the gate refuses to commit on a
   truncated scan, an empty pair list, a failed issuer lookup, or a `per_day` missing today.
2. **The page merges the delta itself.** It already fetches the last ~25h of launches for the
   expiry board, so every event newer than the file's `to_block` is folded into the counts at
   **zero extra cost**. This closes the gap between rebuilds *and* covers a rebuild that never
   ran — GitHub silently drops cron slots and records nothing when it does.
3. **It says so when it cannot.** If the file predates the window's oldest block there is a hole
   the merge cannot see, and the page prints *"stale: the gap since then is wider than the live
   window"* rather than showing incomplete counts as current.

So the failure mode is a visible warning, never a quietly wrong number. `Expiring next` and the
ticker search read the chain directly on every load and cannot go stale at all.

`build_counts.py` is **stdlib-only and standalone** — no pip install, and no dependency on the
research repo — so CI cannot break on an import. It carries its own `eth_getLogs` that bisects on
the log cap and distinguishes a retryable transport blip from a real answer about the request.

## Previewing it

The tool makes outbound calls to the chain, so it needs a normal origin. `file://` and sandboxed
iframes with a restrictive `connect-src` (Claude's artifact preview among them) will fail with
**"Failed to fetch"** — that is the sandbox, not the page. To see it work for real:

```sh
cd AMD/site && python3 -m http.server 8899
# open http://127.0.0.1:8899/registry.html
```

Verified 2026-09-03: the exact batched request this page sends returns the AMD record from a
`http://127.0.0.1:8899` origin. The RPC sends `access-control-allow-origin: *`, so any real host works.

## Before launch

- `index.html` → **Contract address** row says *Pending incorporation*. Replace it.
- `index.html` → `Trade $AMD` button points at `app.long.xyz`; swap for the token's page once live.
- Add `og:url` and a real social card image if you want link previews to look right.

## Ticker rules (probed against the contract)

Letters only, **1–15 characters**, case-insensitive. A digit or punctuation **reverts** rather than
returning "unavailable" — the tool reports that as a separate outcome, because it is one.

**A reservation is not ownership.** It is a 24h anti-copycat window. When it lapses the ticker is
free to anyone again, including while a token still trades under it.
