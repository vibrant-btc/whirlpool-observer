<div align="center">
  <br>
  <br>
  <h1>Whirlpool.Observer</h1>
  <p>
    <strong>A self-hostable Bitcoin blockchain reader for Whirlpool activity.</strong>
  </p>
  <p>
    Whirlpool.Observer reads Bitcoin's open distributed ledger, identifies strict Whirlpool-style transaction patterns, and presents the results in a clean dashboard.
  </p>
</div>

---

## What is Whirlpool.Observer?

Whirlpool.Observer is an open-source tool that anyone can run.

It is useful if you want to self-host a dashboard that follows:

- Whirlpool 0.025 BTC and 0.25 BTC pool activity.
- Strict TX0 transactions.
- Unmixed premix outputs waiting to enter Whirlpool.
- Tracked unspent Whirlpool postmix outputs.
- Whirlpool CoinJoin cycles and their observed input TX0 metadata.

---

## Dashboard

The web dashboard shows:

- sync progress,
- total BTC in Whirlpool,
- total unspent postmix in Whirlpool,
- total CoinJoin cycles,
- per-pool metrics,
- live charts,
- Whirlpool cycle browsing,
- TX0 browsing and filtering,
- mobile-friendly cards,
- selectable transaction-link destination: `am-i.exposed` or `mempool.space`.

---

## Quick start

### 1. Build

```bash
docker compose build
```

### 2. Run

```bash
docker compose up -d
```

### 3. Open

```text
http://localhost:8080
```

### 4. View logs

```bash
docker compose logs -f whirlpool-observer
```

### 5. Stop

```bash
docker compose stop
```

### 6. Remove container/network but keep data

```bash
docker compose down
```

Your database and generated reports remain in the local `data` and `reports` directories.

---

## Configuration

The default configuration works without API keys.

| Setting | Default | Meaning |
|---|---|---|
| `MEMPOOL_API_URL` | `https://mempool.space/api` | Primary Esplora-compatible blockchain API. |
| `MEMPOOL_FALLBACK_API_URL` | `https://blockstream.info/api` | Fallback API if the primary source fails. |
| `WHIRLPOOL_WEB_PORT` | `8080` | Host port for the dashboard. |
| `WHIRLPOOL_RESCAN_HOURS` | `12` | How often to update data after reaching chain tip. |

---

## Public URL and onion support

If you publicly expose Whirlpool.Observer, rebuild with your public URL so social previews use the right absolute links:

```bash
WHIRLPOOL_PUBLIC_URL=https://observer.example.com docker compose build --no-cache
```

If you run behind a Tor hidden service, you can also bake in an Onion-Location header:

```bash
WHIRLPOOL_ONION_LOCATION=http://exampleexampleexampleexampleexampleexampleexampleexample.onion docker compose build --no-cache
```

Both values are build-time settings. Rebuild after changing them.

---

## Persistent files

Docker Compose bind-mounts two local directories:

| Local path | Container path | Purpose |
|---|---|---|
| `./data` | `/data` | SQLite database and scan state. |
| `./reports` | `/reports` | CSV reports and generated PNG charts. |

This lets the scanner resume after container rebuilds, restarts, and removals.

---

## Reports

When the scanner reaches the current chain tip, it refreshes TX0 and Whirlpool states and writes report files into `reports`.

Generated report names look like:

```text
whirlpool_simplereport_YYYYMMDD_HHMMSS.csv
whirlpool_report_YYYYMMDD_HHMMSS.csv
whirlpool_capacity_chart_YYYYMMDD_HHMMSS.png
whirlpool_utxo_chart_YYYYMMDD_HHMMSS.png
```

The live dashboard does not depend on these static files; it reads directly from the local database.

---

## How it works in one paragraph

Whirlpool.Observer downloads raw Bitcoin blocks in order, checks every transaction for strict TX0 and Whirlpool CoinJoin structure, follows known Whirlpool postmix lineage forward, and stores the resulting state in SQLite. Total poolsize is calculated as unmixed premix outputs plus unspent tracked Whirlpool postmix outputs. Postmix capacity only grows when a valid Whirlpool cycle spends tracked Whirlpool UTXOs from the correct pool.

For a more readable explanation, read [Explainer](explainer.md).

---

## Support

Whirlpool.Observer is free and open-source. Donations are optional and appreciated.

Finde more info in [Whirlpool.Observer's Footer](https://whirlpool.observer).

---

## Credit

Whirlpool.Observer is a tool based on the original work by **Ziya Sadr**:

- Original repository: <https://github.com/Ziya-Sadr/Ashi-Whirlpool-Analysis>

---

## License

MIT License. See `LICENSE` for details.
