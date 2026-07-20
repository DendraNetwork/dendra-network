# Dendra Wallet

A lightweight wallet for the Dendra testnet. **$DNDR is a utility token with no monetary value** (the
network is resettable) — do not use these wallets for anything of value.

## Web wallet (Windows / Linux / macOS — any browser)

`wallet/web/index.html` is a **single self-contained file**. There is nothing to install: open it in a
browser (or host it at `wallet.dendranetwork.com`). It signs and broadcasts locally — **your keys never
leave the browser tab**.

Features:
- Create a new wallet (24-word mnemonic) or import an existing one.
- Show your `dendra1…` address and DNDR balance.
- Send DNDR (MsgSend, signed client-side, broadcast to the public RPC).
- Read-only network view: total supply, registered miners, verification mode.

Configuration (top of the page): the **RPC** (`:26657`) and **REST** (`:1317`) endpoints. Defaults point at
the public network. It relies on [CosmJS](https://github.com/cosmos/cosmjs) loaded from a CDN, so the machine
needs internet the first time.

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
