# Analysis of Ashigaru Whirlpool: Unspent Capacity & Anonymity Sets

**`ashidetector.py`** is a Python-based tool for tracing the lineage of Whirlpool CoinJoin transactions on the Bitcoin blockchain.

It starts from known Whirlpool genesis transactions and follows valid 5-input, 5-output Whirlpool-style CoinJoin descendants to analyze unspent capacity and anonymity sets over time.

> Note: the 0.0025 BTC pool has been removed from this version. The tool now tracks only the 0.25 BTC and 0.025 BTC pools.

---

## How It Works

* **Blockchain Sync:** Syncs with the Bitcoin blockchain via [blockstream.info](https://blockstream.info) to fetch transaction and raw block data.
* **Local Database:** Uses a local SQLite database (`whirlpool.db`) to store sync progress, Whirlpool transactions, and tracked UTXOs. You can stop and resume without losing data.
* **Whirlpool Detection:** Identifies valid Whirlpool CoinJoin descendants as 5-input, 5-output transactions spending tracked anonymity-set UTXOs from a single pool.
* **Anonymity Set Tracking:** Marks tracked UTXOs as spent and adds outputs from valid descendant mixes back into the anonymity set.
* **Reporting:** Generates CSV reports for time-series analysis and charting.

---

## API Keys

No API keys are required.

The tool uses the public unauthenticated `https://blockstream.info/api` endpoints. The existing `apikey` file is not used by the code and can be ignored.

---

## Docker Usage

This repository includes a Docker setup so the tool can run isolated on any server with Docker and Docker Compose installed.

### Persistent directories

Docker Compose bind mounts two host directories into the container:

* `./data` -> `/data`: stores the persistent SQLite database (`whirlpool.db`).
* `./reports` -> `/reports`: stores generated CSV report files.

These directories are created in the repository and can be backed up or copied between servers.

### Build the container

From the repository root:

```bash
docker compose build
```

### Start scanning

Run the scanner in the background:

```bash
docker compose up -d
```

The default container command is:

```bash
python ashidetector.py run
```

### View logs

```bash
docker compose logs -f ashi-whirlpool
```

### Stop the container

```bash
docker compose stop
```

### Start again / resume scanning

```bash
docker compose up -d
```

The tool resumes from the last processed block stored in `./data/whirlpool.db`.

### Stop and remove the container

```bash
docker compose down
```

This removes the container and default network, but it does not delete `./data` or `./reports`.

### Fresh rescan

A fresh rescan deletes and recreates the database tables inside the persistent database:

```bash
docker compose run --rm ashi-whirlpool run --fresh
```

### View stats

```bash
docker compose run --rm ashi-whirlpool stats
```

### Generate reports

Generate a simple report into `./reports`:

```bash
docker compose run --rm ashi-whirlpool simplereport
```

Generate a simple report with a custom block interval:

```bash
docker compose run --rm ashi-whirlpool simplereport --interval 100
```

Generate a detailed report into `./reports`:

```bash
docker compose run --rm ashi-whirlpool report
```

Generate a detailed report with a custom block interval:

```bash
docker compose run --rm ashi-whirlpool report --interval 500
```

---

## Local Python Usage

Docker is recommended for server deployment, but the tool can still be run directly with Python.

### Installation

```bash
git clone https://github.com/Ziya-Sadr/Ashi-Whirlpool-Analysis.git
cd Ashi-Whirlpool-Analysis
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Commands

```bash
python ashidetector.py run
python ashidetector.py run --fresh
python ashidetector.py stats
python ashidetector.py simplereport
python ashidetector.py simplereport --interval 100
python ashidetector.py report
python ashidetector.py report --interval 500
```

By default, local Python runs store `whirlpool.db` and reports in the current directory. You can override this with environment variables:

```bash
WHIRLPOOL_DATA_DIR=./data WHIRLPOOL_REPORTS_DIR=./reports python ashidetector.py run
```

---

## Files Added for Docker

* `Dockerfile`: builds the isolated Python runtime image.
* `docker-compose.yml`: defines the service and bind mounts `./data` and `./reports`.
* `requirements.txt`: lists Python dependencies.
* `.dockerignore`: keeps local databases, reports, virtualenvs, and cache files out of Docker build context.
* `data/.gitkeep`: keeps the persistent data directory in the repository.
* `reports/.gitkeep`: keeps the report output directory in the repository.

---

## License

This project is licensed under the **MIT License**.
See the `LICENSE` file for more details.
