# Whirlpool Analysis Docker

Docker-first fork of **Ashi Whirlpool Analysis**, a Python-based tool for tracing Whirlpool CoinJoin transaction lineage on the Bitcoin blockchain.

This fork is intended to run isolated in Docker on a server. It tracks the 0.25 BTC and 0.025 BTC Whirlpool pools, stores scan state in a bind-mounted SQLite database, and automatically refreshes CSV reports and PNG charts when the scanner catches up to the blockchain tip and after each 12-hour recheck.

---

## Credit / Original Project

This project is based on the original work by **Ziya Sadr**:

* Original repository: <https://github.com/Ziya-Sadr/Ashi-Whirlpool-Analysis>

---

## How It Works

* **Blockchain Sync:** Fetches Bitcoin transaction and raw block data from the public [blockstream.info](https://blockstream.info) API.
* **Local Database:** Stores sync progress, Whirlpool transactions, and tracked UTXOs in `./data/whirlpool.db` on the host.
* **Whirlpool Detection:** Tracks valid 5-input, 5-output Whirlpool-style CoinJoin descendants that spend tracked anonymity-set UTXOs from a single pool.
* **Anonymity Set Tracking:** Marks tracked UTXOs as spent and adds outputs from valid descendant mixes back into the tracked anonymity set.
* **Automatic Reporting:** Writes refreshed CSV reports to `./reports` whenever the scanner reaches the blockchain tip and after every 12-hour recheck.
* **Automatic Charting:** Writes refreshed PNG charts to `./reports` after report generation. Chart failures are logged and do not stop scanning or CSV report generation.

---

## API Keys

No API keys are required.

The tool uses the public unauthenticated `https://blockstream.info/api` endpoints. The existing `apikey` file is not used by the code and can be ignored.

---

## Requirements

Install these on the host/server:

* Docker

---

## Persistent Directories

Docker Compose bind mounts two host directories into the container:

* `./data` -> `/data`: stores the persistent SQLite database, including `whirlpool.db`.
* `./reports` -> `/reports`: stores generated CSV report files.

These directories are kept outside the container so scan progress and reports survive container rebuilds, restarts, and removals.

---

## Build the Container

From the repository root:

```bash
docker compose build
```

---

## Start Scanning

Run the scanner in the background:

```bash
docker compose up -d
```

The container starts the scanner automatically. It resumes from the last processed block stored in `./data/whirlpool.db`.

---

## View Logs

```bash
docker compose logs -f ashi-whirlpool
```

---

## Stop the Container

```bash
docker compose stop
```

This stops the scanner but keeps the container, database, and reports.

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

Reports and charts are generated automatically by the running scanner.

When the scanner reaches the current blockchain tip, it will:

1. Delete old generated CSV and PNG files from `./reports`.
2. Generate a new simple CSV report.
3. Generate a new detailed CSV report.
4. Generate a new combined pool capacity PNG chart.
5. Generate a new total unspent UTXO count PNG chart.
6. Sleep for 12 hours.
7. Recheck the blockchain tip and repeat the report/chart refresh after it catches up again.

Generated files use these filename patterns:

* `whirlpool_simplereport_YYYYMMDD_HHMMSS.csv`
* `whirlpool_report_YYYYMMDD_HHMMSS.csv`
* `whirlpool_capacity_chart_YYYYMMDD_HHMMSS.png`
* `whirlpool_utxo_chart_YYYYMMDD_HHMMSS.png`

The capacity chart has block height increasing left-to-right on the x-axis and shows the 0.25 BTC pool and 0.025 BTC pool as separate colored lines on the same chart. The UTXO chart shows total unspent tracked UTXO count over increasing block height.

PNG chart generation is intentionally isolated inside error handling. If chart rendering fails for any reason, the scanner and CSV report refresh continue running.

---

## View Current Stats

You can run a one-off stats command through Docker Compose:

```bash
docker compose run --rm ashi-whirlpool stats
```

---

## Files Added for Docker

* `Dockerfile`: builds the isolated runtime image.
* `docker-compose.yml`: defines the service and bind mounts `./data` and `./reports`.
* `requirements.txt`: lists runtime dependencies, including `matplotlib` for PNG chart rendering.
* `.dockerignore`: keeps local databases, reports, virtualenvs, and cache files out of the Docker build context.
* `data/.gitkeep`: keeps the persistent data directory in the repository.
* `reports/.gitkeep`: keeps the report output directory in the repository.

---

## License

This project is licensed under the **MIT License**.
See the `LICENSE` file for more details.
