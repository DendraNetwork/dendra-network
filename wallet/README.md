# Dendra Wallet

A lightweight wallet for the Dendra testnet. **$DNDR is a utility token with no monetary value** (the
network is resettable) — do not use these wallets for anything of value.

## Web wallet (Windows / Linux / macOS — any browser)

`wallet/web/` holds the whole wallet: `index.html` plus the `vendor/` bundle it loads. There is nothing to
install, but the page must be **served over HTTP** rather than opened as a `file://` — a browser refuses
the module import otherwise. Serve the directory (`python3 -m http.server`) or deploy it; the site publishes
it at `/wallet/`. It signs and broadcasts locally — **your keys never leave the browser tab**, and the page
makes no third-party request at all.

Features:
- Create a new wallet (24-word mnemonic) or import an existing one.
- Show your `dendra1…` address and DNDR balance.
- Send DNDR (MsgSend, signed client-side, broadcast to the public RPC).
- Read-only network view: total supply, registered miners, verification mode.

Configuration (top of the page): the **RPC** (`:26657`) and **REST** (`:1317`) endpoints. Defaults point at
the public network (`https://api.dendranetwork.com/rpc` and `/rest`).

[CosmJS](https://github.com/cosmos/cosmjs) is **served from this repository**, not from a CDN:
`wallet/web/vendor/cosmjs-0.39.0.js`. That is deliberate — this is the page where a recovery phrase is
typed, and a dynamic `import()` accepts no `integrity` attribute, so a CDN able to serve different bytes on
any load would run them next to the seed with nothing to check them against. The bundle is rebuilt by
`bash wallet/build-vendor.sh` from a committed lockfile, so its SHA-256 is reproducible by a third party.
Two checks in that script bear on the page itself, and it is worth knowing exactly how far they reach: it
refuses to finish unless `wallet/web/index.html` really loads that bundle, and it refuses the CDN this
bundle replaces, `cdn.jsdelivr.net`, **by name**. It does not enumerate other hosts — a second import
added from somewhere else would clear both checks, so review remains the thing that catches that.

## Get testnet DNDR (faucet)

A new wallet holds nothing. Testnet `$DNDR` comes from the public faucet at
`http://api.dendranetwork.com:4500` — **10 DNDR per address, once every 24 h**, with per-IP and global
daily caps.

> **These tokens have no monetary value.** They are given away, never sold; they buy nothing outside the
> protocol; and the testnet is resettable — a reset erases every balance, including yours.

The faucet is proof-of-work gated (each new address costs CPU, which is what makes Sybil addresses
expensive), so it is called from a terminal rather than from the page:

```bash
# difficulty and drip size, straight from the faucet
curl -s http://api.dendranetwork.com:4500/
# → {"status": "ok", "from": "...", "amount": "10000000udndr", "pow_bits": 20}

python3 - <<'EOF'
import hashlib, itertools, json, urllib.request
ADDR, BITS = "dendra1...", 20          # BITS = pow_bits reported above
for n in itertools.count():
    h = hashlib.sha256(("%s:%d" % (ADDR, n)).encode()).digest()
    if int.from_bytes(h, "big") >> (256 - BITS) == 0:
        break
req = urllib.request.Request("http://api.dendranetwork.com:4500/",
    json.dumps({"address": ADDR, "pow": str(n)}).encode(),
    {"Content-Type": "application/json"})
print(urllib.request.urlopen(req).read().decode())
EOF
```

The faucet answers over plain HTTP and is deliberately unauthenticated: nothing secret travels there (a
public address and a nonce). Its reply is not proof of anything either — **confirm the credit against the
chain**, which is what the wallet's balance reads. `429` is a normal answer: the caps fail closed rather
than letting the faucet account be drained.

> **Node operators:** browser wallets require **CORS** on the node. The launch kit enables it automatically
> (`cors_allowed_origins = ["*"]` on the RPC + `--api.enabled-unsafe-cors` on the REST). If you run a custom
> node, enable both or the wallet's requests will be blocked by the browser.

## Desktop app (Windows / Linux)

The web wallet already runs on every desktop through the browser. A native packaged app (single-click,
auto-update) is planned via [Tauri](https://tauri.app/) — it wraps the exact same `web/` front-end into a
small signed binary. Build notes will live in `wallet/desktop/` when that lands.

## Security notes

- Testnet only. Keys are held **in memory in the browser tab**; closing the tab forgets them (import your
  mnemonic to restore). A future version will offer optional encrypted local storage.
- Always verify the recipient address and the amount before sending.
- Never paste a mnemonic that controls real assets into a testnet tool.
