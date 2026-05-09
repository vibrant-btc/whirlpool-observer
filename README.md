# Whirlpool.Observer

Docker-first fork of **Ashi Whirlpool Analysis**, a Python-based tool for tracing Whirlpool CoinJoin transaction lineage on the Bitcoin blockchain.

Whirlpool.Observer runs isolated in Docker on a server. It tracks the 0.25 BTC and 0.025 BTC Whirlpool pools, stores scan state in a bind-mounted SQLite database, exposes a live pure-black web dashboard, and automatically refreshes CSV reports and PNG charts when the scanner catches up to the blockchain tip and after each configurable recheck interval.

---

## Credit / Original Project

This project is based on the original work by **Ziya Sadr**:

* Original repository: <https://github.com/Ziya-Sadr/Ashi-Whirlpool-Analysis>

---

## How It Works

* **Blockchain Sync:** Fetches Bitcoin transaction and raw block data from a mempool.space-compatible REST API.
* **Configurable API Source:** Uses the public `https://mempool.space/api` endpoint by default, but can be pointed at any self-hosted mempool.space API URL.
* **Local Database:** Stores sync progress, Whirlpool transactions, TX0 metadata, tracked UTXOs, and web dashboard data in `./data/whirlpool.db` on the host.
* **Whirlpool Detection:** Tracks valid 5-input, 5-output Whirlpool-style CoinJoin descendants that spend tracked anonymity-set UTXOs from a single pool.
* **TX0 Detection:** Inspects non-remix inputs to identify TX0 transactions, premix outputs, coordinator fee outputs, entered capacity, and fee-efficiency stats.
* **Anonymity Set Tracking:** Marks tracked UTXOs as spent and adds outputs from valid descendant mixes back into the tracked anonymity set.
* **Automatic Reporting:** Writes refreshed CSV reports to `./reports` whenever the scanner reaches the blockchain tip and after every recheck.
* **Automatic Charting:** Writes refreshed PNG charts to `./reports` after report generation. Chart failures are logged and do not stop scanning or CSV report generation.
* **Live Web UI:** Serves **Whirlpool.Observer** on port `8080` by default, with live charts, sync progress, pool stats, TX0/cycle stats, and Whirlpool transaction browsing.
* **Social/Favicon Assets:** Serves favicon and social preview images from `./assets`.

---

## Blockchain Data Source

No API keys are required.

The tool uses a mempool.space-compatible REST API for blockchain data. By default it uses:

```bash
https://mempool.space/api
```

You can point it at your own self-hosted mempool.space instance by setting `MEMPOOL_API_URL` to the full API base URL, including `/api`.

Examples:

```bash
MEMPOOL_API_URL=https://mymempool.example.com/api
MEMPOOL_API_URL=http://192.168.1.50:4080/api
```

The scanner uses these mempool.space-compatible endpoints:

* `/blocks/tip/height` to get the current chain tip height.
* `/block-height/:height` to resolve a block height to a block hash.
* `/block/:hash/raw` to download the complete raw binary block.
* `/tx/:txid` to fetch known genesis transactions and TX0/parent transaction metadata.

Because full raw blocks are downloaded with `/block/:hash/raw`, the 25-transaction pagination limit on `/block/:hash/txs` is not used by this tool.

---

## Requirements

Install these on the host/server:

* Docker
* Docker Compose

---

## Persistent Directories

Docker Compose bind mounts two host directories into the container:

* `./data` -> `/data`: stores the persistent SQLite database, including `whirlpool.db`.
* `./reports` -> `/reports`: stores generated CSV report and PNG chart files.

These directories are kept outside the container so scan progress, enriched TX0 metadata, dashboard data, and reports survive container rebuilds, restarts, and removals.

---

## Build the Container

From the repository root:

```bash
docker compose build
```

### Public social-card URL (build-time)

Social-card crawlers such as Twitter/X, Facebook, Signal, Telegram, and link preview checkers often require absolute URLs in Open Graph and Twitter card metadata. Whirlpool.Observer therefore bakes the public site URL into [`observer.html`](observer.html) during the Docker image build.

If you are only running locally, the default is fine:

```bash
WHIRLPOOL_PUBLIC_URL=http://localhost:8080
```

If you publicly expose Whirlpool.Observer, set the public URL before building:

```bash
WHIRLPOOL_PUBLIC_URL=https://observer.example.com docker compose build --no-cache
```

For a persistent setting, add it to [`.env`](.env):

```bash
WHIRLPOOL_PUBLIC_URL=https://observer.example.com
```

Then rebuild the image:

```bash
docker compose build --no-cache
```

This value is build-time only. If you change it later, rebuild the image before restarting the container.

### Onion-Location header (build-time)

If you run Whirlpool.Observer behind a Tor hidden service, you can also bake in an Onion-Location header at image build time:

```bash
WHIRLPOOL_ONION_LOCATION=http://exampleexampleexampleexampleexampleexampleexampleexample.onion docker compose build --no-cache
```

Or add it to [`.env`](.env):

```bash
WHIRLPOOL_ONION_LOCATION=http://exampleexampleexampleexampleexampleexampleexampleexample.onion
```

Then rebuild:

```bash
docker compose build --no-cache
```

When configured, every HTTP response includes:

```http
Onion-Location: http://exampleexampleexampleexampleexampleexampleexampleexample.onion
```

Leave `WHIRLPOOL_ONION_LOCATION` empty to disable this header. This is also build-time only; rebuild the image after changing it.

---

## Configure a Self-Hosted mempool.space API

The default API URL is set in `docker-compose.yml` as:

```yaml
MEMPOOL_API_URL: ${MEMPOOL_API_URL:-https://mempool.space/api}
```

To use your own self-hosted mempool.space instance for one run, pass the variable before Docker Compose:

```bash
MEMPOOL_API_URL=https://mymempool.example.com/api docker compose up -d
```

For a persistent local setting, create a `.env` file beside `docker-compose.yml`:

```bash
MEMPOOL_API_URL=https://mymempool.example.com/api
```

Then start normally with `docker compose up -d`.

---

## Configure Recheck Interval and Web Port

The scanner rechecks for new blocks every 12 hours by default after it reaches the blockchain tip.

You can change this with `WHIRLPOOL_RESCAN_HOURS`:

```bash
WHIRLPOOL_RESCAN_HOURS=1 docker compose up -d
```

The web UI is exposed on host port `8080` by default. Change it with:

```bash
WHIRLPOOL_WEB_PORT=8081 docker compose up -d
```

These values can also be placed in `.env`:

```bash
MEMPOOL_API_URL=https://mymempool.example.com/api
WHIRLPOOL_RESCAN_HOURS=1
WHIRLPOOL_WEB_PORT=8081
```


## Start Scanning and Open Whirlpool.Observer

Run the scanner and web UI in the background:

```bash
docker compose up -d
```

Open the dashboard in a browser:

```bash
http://localhost:8080
```

If running on a server, replace `localhost` with the server IP or hostname.

The container starts the scanner automatically. It resumes from the last processed block stored in `./data/whirlpool.db`.

---

## Whirlpool.Observer Web UI

The dashboard provides live, interactive visual analysis while the scanner is running:

* Pure-black dashboard theme.
* Favicon loaded from `assets/Ashigaru_Whirlpool_Logo_White.png`.
* Logo displayed next to the Whirlpool.Observer title.
* Open Graph and Twitter/X social preview cards using `assets/social.png`.
* Sync/scanning progress without needing to read logs.
* “Synced to blockheight” state and next update countdown after initial sync.
* Total unspent capacity.
* Total entered capacity detected from TX0 premix outputs.
* Per-pool cycle count, where a cycle means one tracked Whirlpool transaction.
* Per-pool TX0 count.
* Total entered capacity by pool graph.
* Live unspent capacity by pool graph.
* Total UTXOs entered graph.
* Total unspent UTXOs graph.
* Paginated Whirlpool transaction list with pool filtering.
* Clickable Whirlpool transaction IDs that open `http://am-i.exposed/#tx=<txid>`.
* Desktop-only TX0 input and fee-efficiency details.
* Mobile-optimized cycle cards without the extra TX0 detail column.
* Collapsible reference/FAQ section explaining key terms and calculations.

Existing partially-filled databases are supported. New schema tables are created without deleting existing scan progress, and missing input/TX0 metadata is backfilled from already-discovered Whirlpool transactions before normal scanning continues.

---

## Logs

The embedded web server suppresses per-request HTTP access logs to prevent Docker logs from growing rapidly while the browser refreshes live API data.

Docker Compose also limits container log growth:

```yaml
logging:
  driver: "json-file"
  options:
    max-size: "64k"
    max-file: "1"
```

View logs with:

```bash
docker compose logs -f whirlpool-observer
```

---

## Stop the Container

```bash
docker compose stop
```

This stops the scanner and web UI but keeps the container, database, and reports.

The scanner handles `SIGTERM` / `SIGINT` gracefully: it stops between blocks, commits progress, and closes SQLite cleanly.

---

## Start Again / Resume Scanning

```bash
docker compose up -d
```

The scanner resumes from the last processed block in `./data/whirlpool.db`.

---

## Stop and Remove the Container

```bash
docker compose down
```

This removes the container and default Docker network, but it does not delete `./data` or `./reports`.

---

## Reports and Charts

Reports and static PNG charts are generated automatically by the running scanner.

When the scanner reaches the current blockchain tip, it will:

1. Delete old generated CSV and PNG files from `./reports`.
2. Generate a new simple CSV report.
3. Generate a new detailed CSV report.
4. Generate a new combined pool capacity PNG chart.
5. Generate a new total unspent UTXO count PNG chart.
6. Sleep for the configured recheck interval.
7. Recheck the blockchain tip and repeat the report/chart refresh after it catches up again.

Generated files use these filename patterns:

* `whirlpool_simplereport_YYYYMMDD_HHMMSS.csv`
* `whirlpool_report_YYYYMMDD_HHMMSS.csv`
* `whirlpool_capacity_chart_YYYYMMDD_HHMMSS.png`
* `whirlpool_utxo_chart_YYYYMMDD_HHMMSS.png`

The web UI renders interactive charts live from the database and does not depend on these static PNG files.

---

## View Current Stats

You can run a one-off stats command through Docker Compose:

```bash
docker compose run --rm whirlpool-observer stats
```

---

## Files Added for Docker

* [`Dockerfile`](Dockerfile): builds the isolated runtime image, exposes the web UI port, and bakes `WHIRLPOOL_PUBLIC_URL` / `WHIRLPOOL_ONION_LOCATION` into the image at build time.
* [`docker-compose.yml`](docker-compose.yml): defines the service, bind mounts [`data`](data) and [`reports`](reports), maps the web UI port, limits log file growth, and exposes configurable environment variables/build args.
* [`observer.html`](observer.html): serves the Whirlpool.Observer dashboard UI and includes favicon/social-card metadata.
* [`assets/Ashigaru_Whirlpool_Logo_White.png`](assets/Ashigaru_Whirlpool_Logo_White.png): favicon, Apple touch icon, and title logo.
* [`assets/social.png`](assets/social.png): Open Graph and Twitter/X social preview image.
* [`requirements.txt`](requirements.txt): lists runtime dependencies, including `flask` for the web UI and `matplotlib` for PNG chart rendering.
* [`.dockerignore`](.dockerignore): keeps local databases, reports, virtualenvs, and cache files out of the Docker build context.
* [`data/.gitkeep`](data/.gitkeep): keeps the persistent data directory in the repository.
* [`reports/.gitkeep`](reports/.gitkeep): keeps the report output directory in the repository.

---

## Sample DB

/assets/whirlpool.db contains a sample synced database upto Blockheight 948,651 which you may use for testing. It is reccomended that you scan the blockchain yourself however.

## License

This project is licensed under the **MIT License**.
See the `LICENSE` file for more details.
