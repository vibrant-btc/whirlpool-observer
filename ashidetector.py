import argparse
import csv
import json
import logging
import os
import signal
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from bitcoin.core import CBlock

# --- Constants ---
DATA_DIR = os.environ.get("WHIRLPOOL_DATA_DIR", ".")
REPORTS_DIR = os.environ.get("WHIRLPOOL_REPORTS_DIR", ".")
DB_FILE = os.environ.get("WHIRLPOOL_DB_FILE", os.path.join(DATA_DIR, "whirlpool.db"))
MEMPOOL_API_BASE_URL = os.environ.get("MEMPOOL_API_URL", "https://mempool.space/api").rstrip("/")
WEB_HOST = os.environ.get("WHIRLPOOL_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WHIRLPOOL_WEB_PORT", "8080"))
ONION_LOCATION = "__WHIRLPOOL_ONION_LOCATION__".strip()
if ONION_LOCATION.startswith("__"):
    ONION_LOCATION = ""
RESCAN_INTERVAL_HOURS = float(os.environ.get("WHIRLPOOL_RESCAN_HOURS", "12"))
PROCESS_LOOP_DELAY_SECONDS = max(int(RESCAN_INTERVAL_HOURS * 60 * 60), 60)
RETRY_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 5
SATOSHIS_PER_BTC = 100_000_000
WHIRLPOOL_TX_INPUTS = 5
WHIRLPOOL_TX_OUTPUTS = 5
TXID_PREFIX_LENGTH = 8
TX0_PREMIX_EXTRA_SATS_MAX = int(os.environ.get("TX0_PREMIX_EXTRA_SATS_MAX", "100000"))

GENESIS_TXS = {
    "0.25_BTC_Pool": {
        "txid": "7784df1182ab86ee33577b75109bb0f7c5622b9fb91df24b65ab2ab01b27dffa",
        "denomination_sats": int(0.25 * SATOSHIS_PER_BTC),
    },
    "0.025_BTC_Pool": {
        "txid": "737a867727db9a2c981ad622f2fa14b021ce8b1066a001e34fb793f8da833155",
        "denomination_sats": int(0.025 * SATOSHIS_PER_BTC),
    },
}
POOL_LABELS = {
    "0.25_BTC_Pool": "0.25 BTC Pool",
    "0.025_BTC_Pool": "0.025 BTC Pool",
}
POOL_COLORS = {
    "0.25_BTC_Pool": "#f5f5f7",
    "0.025_BTC_Pool": "#8e8e93",
}

# Optimization: The block before which no known post-genesis spends occurred.
NO_SPEND_UNTIL_BLOCK = 899335

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.getLogger("matplotlib").setLevel(logging.WARNING)
logging.getLogger("fontTools").setLevel(logging.WARNING)
STOP_EVENT = threading.Event()


class DatabaseManager:
    """Handles SQLite operations for Whirlpool lineage tracking and web statistics."""

    def __init__(self, db_file: str):
        self.db_file = db_file
        db_dir = os.path.dirname(os.path.abspath(self.db_file))
        os.makedirs(db_dir, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_file, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()

    def setup_db(self, start_block_height: int, fresh_start=False):
        logging.info("Setting up database for Whirlpool.Observer...")
        with self.lock:
            if fresh_start:
                logging.warning("FRESH START: Dropping all existing tables.")
                for table in [
                    "progress", "whirlpool_txs", "anonymity_set_utxos", "tx_inputs",
                    "tx0s", "tx0_premix_outputs"
                ]:
                    self.cursor.execute(f"DROP TABLE IF EXISTS {table}")

            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS progress (
                    key TEXT PRIMARY KEY,
                    value INTEGER
                )
            """)
            self.cursor.execute(
                "INSERT OR IGNORE INTO progress (key, value) VALUES ('last_processed_block_height', ?)",
                (start_block_height - 1,)
            )
            self.cursor.execute("INSERT OR IGNORE INTO progress (key, value) VALUES ('current_tip_height', 0)")
            self.cursor.execute("INSERT OR IGNORE INTO progress (key, value) VALUES ('current_processing_block', 0)")
            self.cursor.execute("INSERT OR IGNORE INTO progress (key, value) VALUES ('last_report_refresh_ts', 0)")

            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS whirlpool_txs (
                    txid TEXT PRIMARY KEY,
                    block_height INTEGER,
                    block_hash TEXT,
                    pool_name TEXT
                )
            """)
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_whirlpool_txs_block_height ON whirlpool_txs(block_height)")
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_whirlpool_txs_pool ON whirlpool_txs(pool_name)")

            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS anonymity_set_utxos (
                    output_id TEXT PRIMARY KEY,
                    txid TEXT,
                    vout INTEGER,
                    value_sats INTEGER,
                    pool_name TEXT,
                    is_spent BOOLEAN DEFAULT 0,
                    spent_in_txid TEXT,
                    spent_in_block_height INTEGER
                )
            """)
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_utxos_txid ON anonymity_set_utxos(txid)")
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_utxos_is_spent ON anonymity_set_utxos(is_spent)")
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_utxos_spent_in ON anonymity_set_utxos(spent_in_txid)")

            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS tx_inputs (
                    whirlpool_txid TEXT,
                    input_index INTEGER,
                    prev_txid TEXT,
                    prev_vout INTEGER,
                    source_type TEXT,
                    pool_name TEXT,
                    value_sats INTEGER,
                    tx0_txid TEXT,
                    tx0_vout INTEGER,
                    PRIMARY KEY (whirlpool_txid, input_index)
                )
            """)
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx_inputs_prev ON tx_inputs(prev_txid, prev_vout)")
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx_inputs_tx0 ON tx_inputs(tx0_txid)")
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx_inputs_pool ON tx_inputs(pool_name)")

            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS tx0s (
                    txid TEXT PRIMARY KEY,
                    pool_name TEXT,
                    block_height INTEGER,
                    block_hash TEXT,
                    coordinator_fee_sats INTEGER,
                    premix_output_count INTEGER,
                    entered_capacity_sats INTEGER,
                    fee_efficiency_pct REAL,
                    first_seen_whirlpool_txid TEXT,
                    observed_at_block INTEGER
                )
            """)
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx0s_pool ON tx0s(pool_name)")
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx0s_observed ON tx0s(observed_at_block)")

            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS tx0_premix_outputs (
                    output_id TEXT PRIMARY KEY,
                    tx0_txid TEXT,
                    vout INTEGER,
                    value_sats INTEGER,
                    pool_name TEXT,
                    spent_in_whirlpool_txid TEXT,
                    spent_input_index INTEGER
                )
            """)
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx0_outputs_tx0 ON tx0_premix_outputs(tx0_txid)")
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx0_outputs_pool ON tx0_premix_outputs(pool_name)")
            self.conn.commit()
        logging.info("Database setup complete.")

    def get_progress(self, key: str) -> Optional[int]:
        with self.lock:
            self.cursor.execute("SELECT value FROM progress WHERE key=?", (key,))
            result = self.cursor.fetchone()
            return result["value"] if result else None

    def update_progress(self, key: str, value: int):
        with self.lock, self.conn:
            self.cursor.execute("REPLACE INTO progress (key, value) VALUES (?, ?)", (key, value))

    def is_db_seeded(self) -> bool:
        with self.lock:
            self.cursor.execute("SELECT 1 FROM whirlpool_txs WHERE txid=?", (GENESIS_TXS["0.25_BTC_Pool"]["txid"],))
            return self.cursor.fetchone() is not None

    def add_whirlpool_tx_with_utxos(self, tx_data: Dict[str, Any], utxos: List[Dict[str, Any]]):
        with self.lock, self.conn:
            self.cursor.execute("""
                INSERT OR IGNORE INTO whirlpool_txs (txid, block_height, block_hash, pool_name)
                VALUES (:txid, :block_height, :block_hash, :pool_name)
            """, tx_data)
            utxo_records = [
                (f"{u['txid']}:{u['vout']}", u["txid"], u["vout"], u["value_sats"], u["pool_name"])
                for u in utxos
            ]
            self.cursor.executemany("""
                INSERT OR IGNORE INTO anonymity_set_utxos (output_id, txid, vout, value_sats, pool_name)
                VALUES (?, ?, ?, ?, ?)
            """, utxo_records)

    def get_unspent_utxo_by_id(self, output_id: str) -> Optional[sqlite3.Row]:
        with self.lock:
            self.cursor.execute("SELECT * FROM anonymity_set_utxos WHERE output_id = ? AND is_spent = 0", (output_id,))
            return self.cursor.fetchone()

    def mark_utxo_as_spent(self, output_id: str, spent_in_txid: str, spent_in_block_height: int):
        with self.lock, self.conn:
            self.cursor.execute("""
                UPDATE anonymity_set_utxos
                SET is_spent = 1, spent_in_txid = ?, spent_in_block_height = ?
                WHERE output_id = ?
            """, (spent_in_txid, spent_in_block_height, output_id))

    def record_whirlpool_input(self, record: Dict[str, Any]):
        with self.lock, self.conn:
            self.cursor.execute("""
                INSERT OR REPLACE INTO tx_inputs
                (whirlpool_txid, input_index, prev_txid, prev_vout, source_type, pool_name, value_sats, tx0_txid, tx0_vout)
                VALUES (:whirlpool_txid, :input_index, :prev_txid, :prev_vout, :source_type, :pool_name, :value_sats, :tx0_txid, :tx0_vout)
            """, record)

    def upsert_tx0(self, tx0: Dict[str, Any], premix_outputs: List[Dict[str, Any]]):
        with self.lock, self.conn:
            self.cursor.execute("""
                INSERT INTO tx0s
                (txid, pool_name, block_height, block_hash, coordinator_fee_sats, premix_output_count,
                 entered_capacity_sats, fee_efficiency_pct, first_seen_whirlpool_txid, observed_at_block)
                VALUES (:txid, :pool_name, :block_height, :block_hash, :coordinator_fee_sats, :premix_output_count,
                        :entered_capacity_sats, :fee_efficiency_pct, :first_seen_whirlpool_txid, :observed_at_block)
                ON CONFLICT(txid) DO UPDATE SET
                    pool_name=excluded.pool_name,
                    block_height=COALESCE(excluded.block_height, tx0s.block_height),
                    block_hash=COALESCE(excluded.block_hash, tx0s.block_hash),
                    coordinator_fee_sats=excluded.coordinator_fee_sats,
                    premix_output_count=excluded.premix_output_count,
                    entered_capacity_sats=excluded.entered_capacity_sats,
                    fee_efficiency_pct=excluded.fee_efficiency_pct,
                    first_seen_whirlpool_txid=COALESCE(tx0s.first_seen_whirlpool_txid, excluded.first_seen_whirlpool_txid),
                    observed_at_block=COALESCE(tx0s.observed_at_block, excluded.observed_at_block)
            """, tx0)
            for output in premix_outputs:
                self.cursor.execute("""
                    INSERT INTO tx0_premix_outputs
                    (output_id, tx0_txid, vout, value_sats, pool_name, spent_in_whirlpool_txid, spent_input_index)
                    VALUES (:output_id, :tx0_txid, :vout, :value_sats, :pool_name, :spent_in_whirlpool_txid, :spent_input_index)
                    ON CONFLICT(output_id) DO UPDATE SET
                        pool_name=excluded.pool_name,
                        spent_in_whirlpool_txid=COALESCE(tx0_premix_outputs.spent_in_whirlpool_txid, excluded.spent_in_whirlpool_txid),
                        spent_input_index=COALESCE(tx0_premix_outputs.spent_input_index, excluded.spent_input_index)
                """, output)

    def get_whirlpool_txs_missing_inputs(self, limit: int = 2000) -> List[sqlite3.Row]:
        """Return Whirlpool transactions whose derived metadata is incomplete.

        This is intentionally broader than only "no inputs": every Whirlpool cycle
        should have five recorded inputs, five tracked outputs, and no unresolved
        input classifications. Existing partially-scanned databases are repaired
        on startup before normal scanning continues.
        """
        with self.lock:
            self.cursor.execute("""
                SELECT
                    w.*,
                    COUNT(DISTINCT i.input_index) AS input_count,
                    COUNT(DISTINCT u.vout) AS output_count,
                    SUM(CASE WHEN i.source_type IS NULL OR i.source_type = 'unknown' THEN 1 ELSE 0 END) AS unknown_input_count
                FROM whirlpool_txs w
                LEFT JOIN tx_inputs i ON i.whirlpool_txid = w.txid
                LEFT JOIN anonymity_set_utxos u ON u.txid = w.txid
                GROUP BY w.txid
                HAVING input_count != 5 OR output_count != 5 OR unknown_input_count > 0
                ORDER BY w.block_height ASC
                LIMIT ?
            """, (limit,))
            return self.cursor.fetchall()

    def get_anonymity_set_stats(self) -> Dict[str, Any]:
        with self.lock:
            self.cursor.execute("""
                SELECT pool_name, COUNT(*) AS count, SUM(value_sats) AS total_sats
                FROM anonymity_set_utxos
                WHERE is_spent = 0
                GROUP BY pool_name
            """)
            return {row["pool_name"]: {"count": row["count"], "total_sats": row["total_sats"] or 0} for row in self.cursor.fetchall()}

    def query_all(self, sql: str, params: Tuple = ()) -> List[sqlite3.Row]:
        with self.lock:
            self.cursor.execute(sql, params)
            return self.cursor.fetchall()

    def query_one(self, sql: str, params: Tuple = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            self.cursor.execute(sql, params)
            return self.cursor.fetchone()

    def close(self):
        with self.lock:
            self.conn.close()


class MempoolClient:
    def __init__(self):
        self.base_url = MEMPOOL_API_BASE_URL
        logging.info(f"Using mempool.space-compatible API base URL: {self.base_url}")

    def _request(self, endpoint: str, is_json=True) -> Optional[Any]:
        url = f"{self.base_url}/{endpoint}"
        for attempt in range(RETRY_ATTEMPTS):
            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                return response.json() if is_json else response
            except requests.exceptions.RequestException as e:
                logging.warning(f"Request failed for {url}: {e}. Attempt {attempt + 1}/{RETRY_ATTEMPTS}.")
                time.sleep(RETRY_DELAY_SECONDS)
        logging.error(f"Failed to fetch data from {url} after {RETRY_ATTEMPTS} attempts.")
        return None

    def get_tip_height(self) -> Optional[int]:
        response = self._request("blocks/tip/height", is_json=False)
        return int(response.text) if response else None

    def get_block_hash(self, height: int) -> Optional[str]:
        response = self._request(f"block-height/{height}", is_json=False)
        return response.text if response else None

    def get_raw_block(self, block_hash: str) -> Optional[bytes]:
        response = self._request(f"block/{block_hash}/raw", is_json=False)
        return response.content if response else None

    def get_transaction(self, txid: str) -> Optional[Dict[str, Any]]:
        return self._request(f"tx/{txid}")


class WhirlpoolTracer:
    def __init__(self, fresh_start: bool = False):
        self.db_manager = DatabaseManager(DB_FILE)
        self.client = MempoolClient()
        self.web_started = False
        self.start_block_height = self._get_earliest_genesis_block_height()
        self.db_manager.setup_db(self.start_block_height, fresh_start)

    def _get_earliest_genesis_block_height(self) -> int:
        logging.info("Determining earliest genesis block height from known transactions...")
        min_height = float("inf")
        for pool_name, genesis_info in GENESIS_TXS.items():
            txid = genesis_info["txid"]
            tx_details = self.client.get_transaction(txid)
            if not tx_details or "status" not in tx_details or not tx_details["status"].get("confirmed"):
                raise SystemExit(f"Could not fetch confirmed genesis tx {txid}")
            height = tx_details["status"]["block_height"]
            logging.info(f"  -> Genesis tx for {pool_name} ({txid}) is in block {height}.")
            min_height = min(min_height, height)
        if min_height == float("inf"):
            raise SystemExit("Could not determine start block.")
        return int(min_height)

    def _seed_database_with_genesis(self):
        logging.info("Database is not seeded. Seeding with genesis transactions...")
        for pool_name, genesis_info in GENESIS_TXS.items():
            txid = genesis_info["txid"]
            tx_details = self.client.get_transaction(txid)
            if not tx_details:
                raise SystemExit(f"Could not fetch genesis tx {txid}")
            tx_data = {
                "txid": tx_details["txid"],
                "block_height": tx_details["status"]["block_height"],
                "block_hash": tx_details["status"]["block_hash"],
                "pool_name": pool_name,
            }
            utxos = [{"txid": tx_details["txid"], "vout": i, "value_sats": vout["value"], "pool_name": pool_name}
                     for i, vout in enumerate(tx_details["vout"])]
            self.db_manager.add_whirlpool_tx_with_utxos(tx_data, utxos)
            self._log_whirlpool_inputs_from_json(tx_details, pool_name, tx_data["block_height"])
        logging.info("All genesis transactions have been seeded.")

    def _classify_and_record_input(self, whirlpool_txid: str, input_index: int, prev_txid: str, prev_vout: int,
                                   pool_name: str, block_height: int, remix_utxo: Optional[sqlite3.Row] = None):
        record = {
            "whirlpool_txid": whirlpool_txid,
            "input_index": input_index,
            "prev_txid": prev_txid,
            "prev_vout": prev_vout,
            "source_type": "remix" if remix_utxo else "unknown",
            "pool_name": pool_name,
            "value_sats": remix_utxo["value_sats"] if remix_utxo else None,
            "tx0_txid": None,
            "tx0_vout": None,
        }

        if remix_utxo:
            self.db_manager.record_whirlpool_input(record)
            return

        parent_tx = self.client.get_transaction(prev_txid)
        if not parent_tx:
            self.db_manager.record_whirlpool_input(record)
            return

        tx0_info = self._analyze_tx0(parent_tx, pool_name, whirlpool_txid, block_height, prev_vout, input_index)
        if tx0_info:
            record["source_type"] = "tx0"
            record["tx0_txid"] = prev_txid
            record["tx0_vout"] = prev_vout
            try:
                record["value_sats"] = parent_tx["vout"][prev_vout]["value"]
            except (IndexError, KeyError):
                pass
        self.db_manager.record_whirlpool_input(record)

    def _analyze_tx0(self, tx_details: Dict[str, Any], pool_name: str, first_seen_whirlpool_txid: str,
                     observed_at_block: int, spent_vout: int, spent_input_index: int) -> Optional[Dict[str, Any]]:
        denom = GENESIS_TXS[pool_name]["denomination_sats"]
        coordinator_fee_sats = int(denom * 0.05)
        fee_outputs = [i for i, vout in enumerate(tx_details.get("vout", [])) if vout.get("value") == coordinator_fee_sats]
        premix_outputs = []
        for i, vout in enumerate(tx_details.get("vout", [])):
            value = int(vout.get("value", 0))
            if denom <= value <= denom + TX0_PREMIX_EXTRA_SATS_MAX:
                premix_outputs.append({
                    "output_id": f"{tx_details['txid']}:{i}",
                    "tx0_txid": tx_details["txid"],
                    "vout": i,
                    "value_sats": value,
                    "pool_name": pool_name,
                    "spent_in_whirlpool_txid": first_seen_whirlpool_txid if i == spent_vout else None,
                    "spent_input_index": spent_input_index if i == spent_vout else None,
                })
        if not fee_outputs and spent_vout not in [p["vout"] for p in premix_outputs]:
            return None
        premix_count = len(premix_outputs)
        entered_capacity_sats = premix_count * denom
        fee_efficiency_pct = (coordinator_fee_sats / entered_capacity_sats * 100.0) if entered_capacity_sats else None
        status = tx_details.get("status", {})
        tx0 = {
            "txid": tx_details["txid"],
            "pool_name": pool_name,
            "block_height": status.get("block_height"),
            "block_hash": status.get("block_hash"),
            "coordinator_fee_sats": coordinator_fee_sats if fee_outputs else 0,
            "premix_output_count": premix_count,
            "entered_capacity_sats": entered_capacity_sats,
            "fee_efficiency_pct": fee_efficiency_pct,
            "first_seen_whirlpool_txid": first_seen_whirlpool_txid,
            "observed_at_block": observed_at_block,
        }
        self.db_manager.upsert_tx0(tx0, premix_outputs)
        return tx0

    def _log_whirlpool_inputs_from_json(self, tx_details: Dict[str, Any], pool_name: str, block_height: int):
        for idx, vin in enumerate(tx_details.get("vin", [])):
            prev_txid = vin.get("txid")
            prev_vout = vin.get("vout")
            if prev_txid is None or prev_vout is None:
                continue
            output_id = f"{prev_txid}:{prev_vout}"
            remix_utxo = self.db_manager.query_one("SELECT * FROM anonymity_set_utxos WHERE output_id = ?", (output_id,))
            self._classify_and_record_input(tx_details["txid"], idx, prev_txid, int(prev_vout), pool_name, block_height, remix_utxo)

    def backfill_missing_input_metadata(self):
        missing = self.db_manager.get_whirlpool_txs_missing_inputs()
        if not missing:
            logging.info("No existing Whirlpool transactions need input/TX0 metadata backfill.")
            return
        total = len(missing)
        logging.info(f"Backfilling input/TX0 metadata for {total} existing Whirlpool transaction(s)...")
        for index, row in enumerate(missing, start=1):
            txid = row["txid"]
            pool_name = row["pool_name"]
            block_height = row["block_height"]
            logging.info(f"Backfill {index}/{total}: fetching Whirlpool tx {txid} ({pool_name}, block {block_height})")
            tx_details = self.client.get_transaction(txid)
            if tx_details:
                logging.info(f"Backfill {index}/{total}: analyzing inputs for {txid}")
                self._log_whirlpool_inputs_from_json(tx_details, pool_name, block_height)
                logging.info(f"Backfill {index}/{total}: completed metadata for {txid}")
            else:
                logging.warning(f"Backfill {index}/{total}: failed to fetch {txid}; it will be retried later")
        logging.info(f"Backfill pass complete. Processed {total} existing Whirlpool transaction(s).")

    def process_block(self, block: CBlock, block_height: int, block_hash: str):
        logging.info(f"Processing block {block_height}...")
        lineage_tx_count = 0
        for tx in block.vtx:
            if tx.is_coinbase():
                continue
            spent_utxo_pools: Set[str] = set()
            spent_utxos_from_set: Dict[int, sqlite3.Row] = {}
            input_refs: List[Tuple[int, str, int]] = []

            for idx, vin in enumerate(tx.vin):
                prev_txid = vin.prevout.hash[::-1].hex()
                prev_vout = vin.prevout.n
                input_refs.append((idx, prev_txid, prev_vout))
                parent_utxo = self.db_manager.get_unspent_utxo_by_id(f"{prev_txid}:{prev_vout}")
                if parent_utxo:
                    spent_utxo_pools.add(parent_utxo["pool_name"])
                    spent_utxos_from_set[idx] = parent_utxo

            if not spent_utxos_from_set:
                continue

            txid = tx.GetTxid()[::-1].hex()
            logging.info(f"  -> Transaction {txid} spends UTXOs from our anonymity set.")
            for utxo in spent_utxos_from_set.values():
                self.db_manager.mark_utxo_as_spent(utxo["output_id"], txid, block_height)

            if len(spent_utxo_pools) > 1:
                logging.warning(f"  -> Tx {txid} mixes UTXOs from multiple pools: {spent_utxo_pools}. Pruning lineage.")
                continue

            if len(tx.vin) == WHIRLPOOL_TX_INPUTS and len(tx.vout) == WHIRLPOOL_TX_OUTPUTS:
                lineage_tx_count += 1
                pool_name = next(iter(spent_utxo_pools))
                tx_data = {"txid": txid, "block_height": block_height, "block_hash": block_hash, "pool_name": pool_name}
                new_utxos = [{"txid": txid, "vout": i, "value_sats": vout.nValue, "pool_name": pool_name}
                             for i, vout in enumerate(tx.vout)]
                self.db_manager.add_whirlpool_tx_with_utxos(tx_data, new_utxos)
                for idx, prev_txid, prev_vout in input_refs:
                    self._classify_and_record_input(txid, idx, prev_txid, prev_vout, pool_name, block_height, spent_utxos_from_set.get(idx))
            else:
                logging.info(f"  -> Tx {txid} is not a 5-in/5-out Whirlpool mix. Pruning this lineage.")
        logging.info(f"Block {block_height} processed. Found {lineage_tx_count} new Whirlpool transaction(s).")

    def run(self, with_web: bool = True):
        logging.info("Starting Whirlpool.Observer scanner...")
        if with_web:
            self.start_web_server()
        if not self.db_manager.is_db_seeded():
            self._seed_database_with_genesis()
        logging.info("Checking existing database for complete Whirlpool cycle metadata before normal scanning...")
        self.backfill_missing_input_metadata()

        try:
            while not STOP_EVENT.is_set():
                last_processed = self.db_manager.get_progress("last_processed_block_height")
                start_block = last_processed + 1 if last_processed is not None else self.start_block_height
                if start_block < NO_SPEND_UNTIL_BLOCK:
                    logging.info(f"Optimization: No known spends before block {NO_SPEND_UNTIL_BLOCK}. Fast-forwarding...")
                    self.db_manager.update_progress("last_processed_block_height", NO_SPEND_UNTIL_BLOCK - 1)
                    start_block = NO_SPEND_UNTIL_BLOCK

                tip_height = self.client.get_tip_height()
                if not tip_height:
                    logging.error("Could not get tip height. Retrying in 1 minute.")
                    if STOP_EVENT.wait(60):
                        break
                    continue
                self.db_manager.update_progress("current_tip_height", tip_height)
                logging.info(f"Current tip height: {tip_height}. Last processed: {last_processed or 'None'}.")

                if start_block > tip_height:
                    self.db_manager.update_progress("current_processing_block", tip_height)
                    self.display_stats()
                    self.refresh_reports()
                    self.db_manager.update_progress("last_report_refresh_ts", int(time.time()))
                    logging.info(f"Caught up to tip. Waiting for {RESCAN_INTERVAL_HOURS:g} hours. Send SIGTERM/SIGINT for graceful shutdown.")
                    if STOP_EVENT.wait(PROCESS_LOOP_DELAY_SECONDS):
                        break
                    continue

                for height in range(start_block, tip_height + 1):
                    if STOP_EVENT.is_set():
                        logging.info("Shutdown requested; stopping before fetching next block so progress remains consistent.")
                        break
                    self.db_manager.update_progress("current_processing_block", height)
                    block_hash = self.client.get_block_hash(height)
                    if not block_hash:
                        logging.error(f"Could not get block hash for height {height}. Skipping.")
                        continue
                    raw_block_data = self.client.get_raw_block(block_hash)
                    if not raw_block_data:
                        logging.error(f"Could not get raw block for {height}. Skipping.")
                        continue
                    block = CBlock.deserialize(raw_block_data)
                    self.process_block(block, height, block_hash)
                    self.db_manager.update_progress("last_processed_block_height", height)
                    if height % 100 == 0:
                        total_scan_blocks = max(tip_height - self.start_block_height, 1)
                        processed_scan_blocks = max(height - self.start_block_height, 0)
                        progress_pct = min((processed_scan_blocks / total_scan_blocks) * 100, 100)
                        logging.info(f"--- Progress: Reached block {height}/{tip_height} ({progress_pct:.2f}%) ---")
        except KeyboardInterrupt:
            logging.info("Detector stopped by user.")
        finally:
            logging.info("Scanner is shutting down gracefully; committing SQLite state and closing database.")
            self.display_stats()
            self.db_manager.close()
            logging.info("Whirlpool.Observer shut down.")

    def display_stats(self):
        logging.info("--- Current Anonymity Set Stats ---")
        stats = self.db_manager.get_anonymity_set_stats()
        if not stats:
            logging.info("No unspent UTXOs in the anonymity set.")
            return
        total_utxos = 0
        total_btc = 0.0
        for pool, data in stats.items():
            btc_value = data["total_sats"] / SATOSHIS_PER_BTC
            total_utxos += data["count"]
            total_btc += btc_value
            logging.info(f"  {pool}: {data['count']} UTXOs ({btc_value:.4f} BTC)")
        logging.info(f"  Total: {total_utxos} UTXOs ({total_btc:.4f} BTC)")

    def refresh_reports(self):
        logging.info("--- Refreshing generated reports and charts ---")
        os.makedirs(REPORTS_DIR, exist_ok=True)
        prefixes = ("whirlpool_report_", "whirlpool_simplereport_", "whirlpool_capacity_chart_", "whirlpool_utxo_chart_")
        for filename in os.listdir(REPORTS_DIR):
            if filename.startswith(prefixes) and filename.endswith((".csv", ".png")):
                try:
                    os.remove(os.path.join(REPORTS_DIR, filename))
                except OSError as e:
                    logging.error(f"Failed to delete old artifact {filename}: {e}")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.generate_simple_report(interval=10, output_file=os.path.join(REPORTS_DIR, f"whirlpool_simplereport_{timestamp}.csv"))
        self.generate_report(interval=1000, output_file=os.path.join(REPORTS_DIR, f"whirlpool_report_{timestamp}.csv"))
        try:
            self.generate_charts(timestamp=timestamp)
        except Exception as e:
            logging.exception(f"Chart generation failed but scanner/report generation will continue: {e}")

    def generate_charts(self, timestamp: str):
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from matplotlib.ticker import FuncFormatter, MaxNLocator
        except ImportError as e:
            logging.error(f"Matplotlib is not available; skipping chart generation: {e}")
            return
        capacity_data = self._build_pool_capacity_chart_data()
        utxo_data = self._build_total_utxo_chart_data()
        if capacity_data:
            self._write_capacity_chart(plt, FuncFormatter, MaxNLocator, capacity_data, os.path.join(REPORTS_DIR, f"whirlpool_capacity_chart_{timestamp}.png"))
        if utxo_data:
            self._write_utxo_chart(plt, FuncFormatter, MaxNLocator, utxo_data, os.path.join(REPORTS_DIR, f"whirlpool_utxo_chart_{timestamp}.png"))

    def _build_pool_capacity_chart_data(self) -> Optional[Dict[str, Any]]:
        rows = self.db_manager.query_all("""
            SELECT t.block_height AS block, u.pool_name, u.value_sats
            FROM anonymity_set_utxos u JOIN whirlpool_txs t ON u.txid = t.txid
            UNION ALL
            SELECT spent_in_block_height AS block, pool_name, -value_sats AS value_sats
            FROM anonymity_set_utxos
            WHERE is_spent = 1 AND spent_in_block_height IS NOT NULL
        """)
        events = [row for row in rows if row["block"] is not None]
        if not events:
            return None
        events.sort(key=lambda x: x["block"])
        pool_names = sorted(GENESIS_TXS.keys())
        cumulative = {pool: 0 for pool in pool_names}
        blocks = []
        series = {pool: [] for pool in pool_names}
        idx = 0
        while idx < len(events):
            block = events[idx]["block"]
            while idx < len(events) and events[idx]["block"] == block:
                if events[idx]["pool_name"] in cumulative:
                    cumulative[events[idx]["pool_name"]] += events[idx]["value_sats"]
                idx += 1
            blocks.append(block)
            for pool in pool_names:
                series[pool].append(cumulative[pool] / SATOSHIS_PER_BTC)
        return {"blocks": blocks, "series": series}

    def _build_total_utxo_chart_data(self) -> Optional[Dict[str, List[int]]]:
        rows = self.db_manager.query_all("""
            SELECT t.block_height AS block, 1 AS utxo_delta
            FROM anonymity_set_utxos u JOIN whirlpool_txs t ON u.txid = t.txid
            UNION ALL
            SELECT spent_in_block_height AS block, -1 AS utxo_delta
            FROM anonymity_set_utxos
            WHERE is_spent = 1 AND spent_in_block_height IS NOT NULL
        """)
        events = [row for row in rows if row["block"] is not None]
        if not events:
            return None
        events.sort(key=lambda x: x["block"])
        blocks, total_utxos = [], []
        cumulative = 0
        idx = 0
        while idx < len(events):
            block = events[idx]["block"]
            while idx < len(events) and events[idx]["block"] == block:
                cumulative += events[idx]["utxo_delta"]
                idx += 1
            blocks.append(block)
            total_utxos.append(cumulative)
        return {"blocks": blocks, "total_utxos": total_utxos}

    def _style_chart(self, ax, title: str, ylabel: str, MaxNLocator):
        ax.set_title(title, fontsize=18, fontweight="bold", pad=18)
        ax.set_xlabel("Block Height", fontsize=12, labelpad=10)
        ax.set_ylabel(ylabel, fontsize=12, labelpad=10)
        ax.grid(True, which="major", axis="y", color="#d8dde6", linewidth=1.0)
        ax.grid(True, which="major", axis="x", color="#eef1f5", linewidth=0.6, alpha=0.7)
        ax.set_facecolor("#fbfcfe")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.tick_params(axis="both", labelsize=10, colors="#2d3748")
        ax.xaxis.set_major_locator(MaxNLocator(nbins=10, integer=True))
        ax.yaxis.set_major_locator(MaxNLocator(nbins=8))
        ax.margins(x=0.01)

    def _write_capacity_chart(self, plt, FuncFormatter, MaxNLocator, chart_data: Dict[str, Any], filename: str):
        fig, ax = plt.subplots(figsize=(14, 7), dpi=160)
        fig.patch.set_facecolor("white")
        for pool_name, values in chart_data["series"].items():
            color = POOL_COLORS.get(pool_name, "#4b5563")
            ax.plot(chart_data["blocks"], values, drawstyle="steps-post", linewidth=2.5, color=color, label=POOL_LABELS.get(pool_name, pool_name))
            ax.fill_between(chart_data["blocks"], values, step="post", alpha=0.08, color=color)
        self._style_chart(ax, "Whirlpool Unspent Capacity by Pool", "Unspent Capacity (BTC)", MaxNLocator)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.2f}"))
        ax.legend(loc="upper right", frameon=True, facecolor="white", edgecolor="#d8dde6")
        fig.tight_layout()
        fig.savefig(filename, bbox_inches="tight")
        plt.close(fig)

    def _write_utxo_chart(self, plt, FuncFormatter, MaxNLocator, chart_data: Dict[str, List[int]], filename: str):
        fig, ax = plt.subplots(figsize=(14, 7), dpi=160)
        fig.patch.set_facecolor("white")
        ax.plot(chart_data["blocks"], chart_data["total_utxos"], drawstyle="steps-post", linewidth=2.5, color="#16a34a", label="Total Unspent UTXOs")
        ax.fill_between(chart_data["blocks"], chart_data["total_utxos"], step="post", alpha=0.10, color="#16a34a")
        self._style_chart(ax, "Whirlpool Total Unspent UTXO Count", "Unspent UTXOs", MaxNLocator)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{int(value):,}"))
        ax.legend(loc="upper right", frameon=True, facecolor="white", edgecolor="#d8dde6")
        fig.tight_layout()
        fig.savefig(filename, bbox_inches="tight")
        plt.close(fig)

    def generate_report(self, interval=1000, output_file=None):
        rows = self._capacity_rows(interval)
        if output_file and rows:
            pool_names = sorted(GENESIS_TXS.keys())
            header = ["end_block", "total_unspent_btc"] + [f"delta_{name}_btc" for name in pool_names]
            self._write_chart_report_to_csv(output_file, header, rows)

    def _capacity_rows(self, interval: int) -> List[Dict[str, Any]]:
        rows = self.db_manager.query_all("""
            SELECT t.block_height AS block, u.pool_name, u.value_sats
            FROM anonymity_set_utxos u JOIN whirlpool_txs t ON u.txid = t.txid
            UNION ALL
            SELECT spent_in_block_height AS block, pool_name, -value_sats AS value_sats
            FROM anonymity_set_utxos WHERE is_spent = 1 AND spent_in_block_height IS NOT NULL
        """)
        events = [r for r in rows if r["block"] is not None]
        if not events:
            return []
        events.sort(key=lambda x: x["block"])
        pool_names = sorted(GENESIS_TXS.keys())
        min_block, max_block = events[0]["block"], events[-1]["block"]
        report_rows, cumulative_stats = [], {name: 0 for name in pool_names}
        event_idx = 0
        for end_block in range((min_block // interval + 1) * interval, (max_block // interval + 2) * interval, interval):
            period_delta_stats = {name: 0 for name in pool_names}
            while event_idx < len(events) and events[event_idx]["block"] < end_block:
                pool = events[event_idx]["pool_name"]
                if pool in cumulative_stats:
                    cumulative_stats[pool] += events[event_idx]["value_sats"]
                    period_delta_stats[pool] += events[event_idx]["value_sats"]
                event_idx += 1
            if not any(period_delta_stats.values()) and sum(cumulative_stats.values()) == 0:
                continue
            row = {"end_block": end_block, "total_unspent_btc": sum(cumulative_stats.values()) / SATOSHIS_PER_BTC}
            for name in pool_names:
                row[f"delta_{name}_btc"] = period_delta_stats[name] / SATOSHIS_PER_BTC
            report_rows.append(row)
        return report_rows

    def _write_chart_report_to_csv(self, filename: str, header: List[str], rows: List[Dict[str, Any]]):
        os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)
        with open(filename, "w", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=header)
            writer.writeheader()
            writer.writerows(rows)
        logging.info(f"Report saved to {filename}")

    def generate_simple_report(self, interval=10, output_file=None):
        rows = self.db_manager.query_all("""
            SELECT t.block_height AS block, u.value_sats, 1 AS utxo_delta
            FROM anonymity_set_utxos u JOIN whirlpool_txs t ON u.txid = t.txid
            UNION ALL
            SELECT spent_in_block_height AS block, -value_sats AS value_sats, -1 AS utxo_delta
            FROM anonymity_set_utxos WHERE is_spent = 1 AND spent_in_block_height IS NOT NULL
        """)
        events = [r for r in rows if r["block"] is not None]
        if not events:
            return
        events.sort(key=lambda x: x["block"])
        header = ["end_block", "total_unspent_btc", "total_unspent_utxos", "net_change_btc", "net_change_utxos"]
        report_rows, cumulative_sats, cumulative_utxos, event_idx = [], 0, 0, 0
        min_block, max_block = events[0]["block"], events[-1]["block"]
        for end_block in range((min_block // interval + 1) * interval, (max_block // interval + 2) * interval, interval):
            period_delta_sats, period_delta_utxos = 0, 0
            while event_idx < len(events) and events[event_idx]["block"] < end_block:
                cumulative_sats += events[event_idx]["value_sats"]
                cumulative_utxos += events[event_idx]["utxo_delta"]
                period_delta_sats += events[event_idx]["value_sats"]
                period_delta_utxos += events[event_idx]["utxo_delta"]
                event_idx += 1
            if period_delta_sats == 0 and cumulative_sats == 0 and not any(r["net_change_btc"] != 0 for r in report_rows):
                continue
            report_rows.append({
                "end_block": end_block,
                "total_unspent_btc": cumulative_sats / SATOSHIS_PER_BTC,
                "total_unspent_utxos": cumulative_utxos,
                "net_change_btc": period_delta_sats / SATOSHIS_PER_BTC,
                "net_change_utxos": period_delta_utxos,
            })
        if output_file and report_rows:
            self._write_chart_report_to_csv(output_file, header, report_rows)

    # --- Web API and UI ---
    def start_web_server(self):
        if self.web_started:
            return
        self.web_started = True
        try:
            from flask import Flask, jsonify, request, send_from_directory
        except ImportError:
            logging.error("Flask is not installed; web GUI disabled.")
            return

        app = Flask(__name__)
        tracer = self

        @app.after_request
        def add_onion_location_header(response):
            if ONION_LOCATION:
                response.headers["Onion-Location"] = ONION_LOCATION
            return response

        if ONION_LOCATION:
            logging.info(f"Onion-Location header enabled: {ONION_LOCATION}")

        @app.route("/")
        def index():
            template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "observer.html")
            with open(template_path, "r", encoding="utf-8") as template_file:
                return template_file.read()

        @app.route("/assets/<path:filename>")
        def assets(filename):
            assets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
            return send_from_directory(assets_dir, filename)

        @app.route("/favicon.ico")
        def favicon():
            assets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
            return send_from_directory(assets_dir, "Ashigaru_Whirlpool_Logo_White.png", mimetype="image/png")

        @app.route("/api/summary")
        def api_summary():
            return jsonify(tracer.build_summary())

        @app.route("/api/charts")
        def api_charts():
            return jsonify(tracer.build_live_charts())

        @app.route("/api/txs")
        def api_txs():
            pool = request.args.get("pool")
            page = max(int(request.args.get("page", 1)), 1)
            per_page = min(max(int(request.args.get("per_page", 25)), 1), 25)
            return jsonify(tracer.list_whirlpool_txs(pool=pool, page=page, per_page=per_page))

        @app.route("/api/tx/<txid>")
        def api_tx(txid):
            return jsonify(tracer.tx_detail(txid))

        def run_app():
            from socketserver import ThreadingMixIn
            from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

            class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
                daemon_threads = True

            class QuietWSGIRequestHandler(WSGIRequestHandler):
                def log_message(self, _format, *args):
                    return

            logging.info(f"Starting Whirlpool.Observer web UI on http://{WEB_HOST}:{WEB_PORT}")
            logging.info("HTTP access logging is disabled to avoid noisy Docker logs; reverse proxy recommended for public internet exposure.")
            with make_server(WEB_HOST, WEB_PORT, app, server_class=ThreadingWSGIServer, handler_class=QuietWSGIRequestHandler) as server:
                server.serve_forever()

        threading.Thread(target=run_app, daemon=True).start()

    def build_summary(self) -> Dict[str, Any]:
        last_processed = self.db_manager.get_progress("last_processed_block_height") or 0
        tip = self.db_manager.get_progress("current_tip_height") or 0
        current = self.db_manager.get_progress("current_processing_block") or last_processed
        last_refresh_ts = self.db_manager.get_progress("last_report_refresh_ts") or 0
        total_scan_blocks = max((tip or last_processed) - self.start_block_height, 1)
        processed_scan_blocks = max((last_processed or current) - self.start_block_height, 0)
        progress_pct = min((processed_scan_blocks / total_scan_blocks) * 100, 100) if tip else 0
        is_synced = bool(tip and last_processed >= tip)
        next_update_seconds = max(int(last_refresh_ts + PROCESS_LOOP_DELAY_SECONDS - time.time()), 0) if is_synced and last_refresh_ts else None
        pools = []
        for pool in sorted(GENESIS_TXS.keys()):
            unspent = self.db_manager.query_one("SELECT COUNT(*) AS count, COALESCE(SUM(value_sats),0) AS sats FROM anonymity_set_utxos WHERE pool_name=? AND is_spent=0", (pool,))
            cycles = self.db_manager.query_one("SELECT COUNT(*) AS count FROM whirlpool_txs WHERE pool_name=?", (pool,))
            entered = self.db_manager.query_one("SELECT COUNT(*) AS tx0s, COALESCE(SUM(entered_capacity_sats),0) AS sats, AVG(fee_efficiency_pct) AS avg_fee_eff FROM tx0s WHERE pool_name=?", (pool,))
            pools.append({
                "pool": pool,
                "label": POOL_LABELS.get(pool, pool),
                "color": POOL_COLORS.get(pool, "#888"),
                "unspent_btc": (unspent["sats"] or 0) / SATOSHIS_PER_BTC,
                "unspent_utxos": unspent["count"] or 0,
                "entered_btc": (entered["sats"] or 0) / SATOSHIS_PER_BTC,
                "tx0_count": entered["tx0s"] or 0,
                "avg_fee_efficiency_pct": entered["avg_fee_eff"] or 0,
                "cycles": cycles["count"] or 0,
            })
        return {
            "title": "Whirlpool.Observer",
            "api_url": MEMPOOL_API_BASE_URL,
            "rescan_hours": RESCAN_INTERVAL_HOURS,
            "start_block_height": self.start_block_height,
            "last_processed_block": last_processed,
            "current_processing_block": current,
            "tip_height": tip,
            "progress_pct": progress_pct,
            "is_synced": is_synced,
            "next_update_seconds": next_update_seconds,
            "last_report_refresh_ts": last_refresh_ts,
            "pools": pools,
        }

    def build_live_charts(self) -> Dict[str, Any]:
        capacity = self._build_pool_capacity_chart_data() or {"blocks": [], "series": {}}
        utxos = self._build_total_utxo_chart_data() or {"blocks": [], "total_utxos": []}
        entered_rows = self.db_manager.query_all("""
            SELECT COALESCE(observed_at_block, block_height) AS block, pool_name, entered_capacity_sats, premix_output_count
            FROM tx0s WHERE entered_capacity_sats > 0 ORDER BY block ASC
        """)
        entered_blocks = []
        entered_series = {pool: [] for pool in sorted(GENESIS_TXS.keys())}
        entered_utxo_blocks = []
        entered_utxo_totals = []
        cum = {pool: 0 for pool in sorted(GENESIS_TXS.keys())}
        cum_utxos = 0
        for row in entered_rows:
            if row["pool_name"] in cum:
                cum[row["pool_name"]] += row["entered_capacity_sats"] or 0
                cum_utxos += row["premix_output_count"] or 0
                entered_blocks.append(row["block"] or 0)
                entered_utxo_blocks.append(row["block"] or 0)
                entered_utxo_totals.append(cum_utxos)
                for pool in cum:
                    entered_series[pool].append(cum[pool] / SATOSHIS_PER_BTC)
        return {
            "capacity": capacity,
            "utxos": utxos,
            "entered": {"blocks": entered_blocks, "series": entered_series},
            "entered_utxos": {"blocks": entered_utxo_blocks, "total_utxos": entered_utxo_totals},
        }

    def list_whirlpool_txs(self, pool: Optional[str] = None, page: int = 1, per_page: int = 25) -> Dict[str, Any]:
        page = max(page, 1)
        per_page = min(max(per_page, 1), 25)
        offset = (page - 1) * per_page
        where = "WHERE w.pool_name = ?" if pool in GENESIS_TXS else ""
        params: Tuple[Any, ...] = (pool,) if where else ()
        total_row = self.db_manager.query_one(f"SELECT COUNT(*) AS count FROM whirlpool_txs w {where}", params)
        rows = self.db_manager.query_all(f"""
            SELECT w.txid, w.block_height, w.pool_name,
                   GROUP_CONCAT(
                       CASE
                           WHEN i.source_type = 'tx0' THEN
                               i.tx0_txid || '|' || COALESCE(printf('%.2f', t.fee_efficiency_pct), 'n/a')
                           ELSE NULL
                       END,
                       ';;'
                   ) AS tx0_inputs
            FROM whirlpool_txs w
            LEFT JOIN tx_inputs i ON i.whirlpool_txid = w.txid
            LEFT JOIN tx0s t ON t.txid = i.tx0_txid
            {where}
            GROUP BY w.txid
            ORDER BY w.block_height DESC
            LIMIT ? OFFSET ?
        """, params + (per_page, offset))
        items = []
        for row in rows:
            tx0_inputs = []
            if row["tx0_inputs"]:
                for item in row["tx0_inputs"].split(";;"):
                    if not item:
                        continue
                    txid, _, efficiency = item.partition("|")
                    tx0_inputs.append({"txid": txid, "fee_efficiency_pct": efficiency})
            items.append({
                "txid": row["txid"],
                "block_height": row["block_height"],
                "pool_name": row["pool_name"],
                "pool_label": POOL_LABELS.get(row["pool_name"], row["pool_name"]),
                "pool_color": POOL_COLORS.get(row["pool_name"], "#8e8e93"),
                "tx0_inputs": tx0_inputs,
                "am_i_exposed_url": f"http://am-i.exposed/#tx={row['txid']}",
            })
        return {
            "items": items,
            "page": page,
            "per_page": per_page,
            "total": total_row["count"] if total_row else 0,
            "total_pages": max(((total_row["count"] if total_row else 0) + per_page - 1) // per_page, 1),
        }

    def tx_detail(self, txid: str) -> Dict[str, Any]:
        tx = self.db_manager.query_one("SELECT * FROM whirlpool_txs WHERE txid=?", (txid,))
        inputs = self.db_manager.query_all("SELECT * FROM tx_inputs WHERE whirlpool_txid=? ORDER BY input_index", (txid,))
        return {"tx": dict(tx) if tx else None, "inputs": [dict(i) for i in inputs], "am_i_exposed_url": f"http://am-i.exposed/#tx={txid}"}


WHIRLPOOL_OBSERVER_HTML = r"""
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Whirlpool.Observer</title><script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root{color-scheme:dark;--bg:#000;--panel:#0a0a0a;--panel2:#111;--panel3:#151515;--line:#242424;--line2:#333;--text:#f5f5f7;--muted:#8e8e93;--muted2:#b5b5bb;--blue:#4b8dff;--orange:#ff9f0a;--green:#30d158;--red:#ff453a}*{box-sizing:border-box}html,body{min-height:100%;background:#000}body{margin:0;background:#000;color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",system-ui,sans-serif}.wrap{width:min(1480px,100%);margin:0 auto;padding:28px}.hero{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;margin-bottom:22px}.title{margin:0;font-size:52px;line-height:.95;font-weight:820;letter-spacing:-.055em}.subtitle{color:var(--muted);font-size:15px;margin-top:10px}.grid{display:grid;gap:14px}.cards{grid-template-columns:repeat(4,minmax(0,1fr))}.poolcards{grid-template-columns:repeat(2,minmax(0,1fr));margin-top:14px}.charts{grid-template-columns:1fr 1fr;margin-top:14px}.card,.chart,.table{background:var(--panel);border:1px solid var(--line);border-radius:24px;padding:20px}.card{min-width:0}.label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.12em}.value{font-size:32px;line-height:1.05;font-weight:760;letter-spacing:-.04em;margin-top:8px;overflow-wrap:anywhere}.small{font-size:13px;color:var(--muted);margin-top:8px}.pool-title{display:flex;align-items:center;justify-content:space-between;gap:10px}.pool-dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:8px}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px}.metric{background:var(--panel2);border:1px solid var(--line);border-radius:16px;padding:12px}.metric .n{font-weight:740;font-size:20px;margin-top:4px}.progress{height:10px;background:#171717;border-radius:999px;overflow:hidden;margin-top:14px}.bar{height:100%;background:#f5f5f7;border-radius:999px}.chart h2,.table h2{margin:0 0 14px;font-size:21px;letter-spacing:-.03em}.chart canvas{max-height:390px}.table-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}.toggles{display:flex;gap:8px;flex-wrap:wrap}.toggle,.pager button{cursor:pointer;background:var(--panel2);border:1px solid var(--line2);border-radius:999px;color:var(--text);padding:9px 12px;font-weight:650}.toggle.active{background:#f5f5f7;color:#000;border-color:#f5f5f7}.pager{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:13px}.pager button:disabled{opacity:.35;cursor:not-allowed}.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}table{width:100%;border-collapse:collapse;min-width:620px}td,th{border-bottom:1px solid var(--line);padding:13px 8px;text-align:left;color:#e5e5ea}th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.1em}.mono{font-family:"SF Mono",ui-monospace,Menlo,monospace}.txlink{color:#f5f5f7;text-decoration:none}.txlink:hover{text-decoration:underline}.mobile-cycle-card{display:none}.empty{color:var(--muted);padding:18px 0}@media(max-width:1100px){.cards,.poolcards,.charts{grid-template-columns:1fr 1fr}.title{font-size:44px}}@media(max-width:760px){.wrap{padding:18px 12px}.hero{display:block}.title{font-size:38px}.subtitle{font-size:13px}.cards,.poolcards,.charts{grid-template-columns:1fr}.card,.chart,.table{border-radius:20px;padding:16px}.value{font-size:28px}.metrics{grid-template-columns:1fr}.table-head{display:block}.toggles{margin:12px 0}.pager{justify-content:space-between;margin-top:12px}.desktop-table{display:none}.mobile-cycle-card{display:block;background:var(--panel2);border:1px solid var(--line);border-radius:16px;padding:12px;margin:10px 0}.mobile-cycle-card a{display:block;margin-top:6px;word-break:break-all}.chart canvas{max-height:310px}}
</style></head><body><div class="wrap"><div class="hero"><div><h1 class="title">Whirlpool.Observer</h1><div class="subtitle">Live Ashigaru Whirlpool lineage, TX0, capacity, cycle and anonymity-set explorer.</div></div></div>
<div class="grid cards"><div class="card"><div class="label">Sync progress</div><div class="value" id="progressValue">0%</div><div class="progress"><div class="bar" id="progressBar" style="width:0%"></div></div><div class="small" id="heightText"></div></div><div class="card"><div class="label">Total unspent</div><div class="value" id="totalUnspent">0 BTC</div></div><div class="card"><div class="label">Total entered</div><div class="value" id="totalEntered">0 BTC</div></div><div class="card"><div class="label">Total cycles</div><div class="value" id="totalCycles">0</div></div></div>
<div class="grid poolcards" id="poolCards"></div>
<div class="grid charts"><div class="chart"><h2>0.25 BTC Pool: Entered vs Unspent</h2><canvas id="donut0250" height="220"></canvas></div><div class="chart"><h2>0.025 BTC Pool: Entered vs Unspent</h2><canvas id="donut0025" height="220"></canvas></div></div>
<div class="grid charts"><div class="chart"><h2>Total Entered Capacity by Pool</h2><canvas id="enteredChart" height="130"></canvas></div><div class="chart"><h2>Live Unspent Capacity by Pool</h2><canvas id="lineChart" height="130"></canvas></div></div>
<div class="grid charts"><div class="chart"><h2>Total Unspent UTXOs</h2><canvas id="utxoChart" height="150"></canvas></div><div class="chart"><h2>Current Unspent Pool Breakdown</h2><canvas id="poolBreakdown" height="220"></canvas></div></div>
<div class="table" style="margin-top:14px"><div class="table-head"><h2>Whirlpool Cycles</h2><div><div class="toggles" id="poolToggles"></div><div class="pager"><button id="prevPage">Prev</button><span id="pageText">Page 1</span><button id="nextPage">Next</button></div></div></div><div class="table-scroll desktop-table"><table><thead><tr><th>Height</th><th>Pool</th><th>TXID</th></tr></thead><tbody id="txRows"></tbody></table></div><div id="txCards"></div></div></div>
<script>
let charts={},summaryCache=null,currentPool='all',currentPage=1,totalPages=1;const fmt=n=>Number(n||0).toLocaleString(undefined,{maximumFractionDigits:4});const intfmt=n=>Number(n||0).toLocaleString();
function labelFor(pool){let p=(summaryCache?.pools||[]).find(x=>x.pool===pool);return p?p.label:pool}function colorFor(pool){let p=(summaryCache?.pools||[]).find(x=>x.pool===pool);return p?p.color:'#8e8e93'}
function chart(id,type,data,options={}){if(charts[id])charts[id].destroy();charts[id]=new Chart(document.getElementById(id),{type,data,options:{responsive:true,maintainAspectRatio:true,plugins:{legend:{labels:{color:'#e5e5ea',usePointStyle:true}},tooltip:{mode:'index',intersect:false}},scales:type==='doughnut'?{}:{x:{ticks:{color:'#8e8e93',maxTicksLimit:7},grid:{color:'#1c1c1e'}},y:{ticks:{color:'#8e8e93'},grid:{color:'#1c1c1e'}}},...options}})}
function setPool(pool){currentPool=pool;currentPage=1;loadTxs()}function page(delta){currentPage=Math.min(Math.max(1,currentPage+delta),totalPages);loadTxs()}
async function loadTxs(){let q=currentPool==='all'?'':`&pool=${encodeURIComponent(currentPool)}`;let data=await fetch(`/api/txs?page=${currentPage}&per_page=25${q}`).then(r=>r.json());totalPages=data.total_pages||1;document.getElementById('pageText').textContent=`Page ${data.page} / ${totalPages}`;document.getElementById('prevPage').disabled=data.page<=1;document.getElementById('nextPage').disabled=data.page>=totalPages;let rows=data.items||[];document.getElementById('txRows').innerHTML=rows.length?rows.map(t=>`<tr><td>${t.block_height}</td><td><span class="pool-dot" style="background:${t.pool_color}"></span>${t.pool_label}</td><td class="mono"><a class="txlink" target="_blank" href="${t.am_i_exposed_url}">${t.txid}</a></td></tr>`).join(''):`<tr><td colspan="3" class="empty">No cycles found for this filter.</td></tr>`;document.getElementById('txCards').innerHTML=rows.length?rows.map(t=>`<div class="mobile-cycle-card"><div><span class="pool-dot" style="background:${t.pool_color}"></span>${t.pool_label} · block ${t.block_height}</div><a class="txlink mono" target="_blank" href="${t.am_i_exposed_url}">${t.txid}</a></div>`).join(''):`<div class="mobile-cycle-card empty">No cycles found for this filter.</div>`}
function renderSummary(s){summaryCache=s;document.getElementById('progressValue').textContent=s.progress_pct.toFixed(2)+'%';document.getElementById('progressBar').style.width=s.progress_pct+'%';document.getElementById('heightText').textContent=`${s.last_processed_block} / ${s.tip_height}`;let tu=0,te=0,cy=0;s.pools.forEach(p=>{tu+=p.unspent_btc;te+=p.entered_btc;cy+=p.cycles});document.getElementById('totalUnspent').textContent=fmt(tu)+' BTC';document.getElementById('totalEntered').textContent=fmt(te)+' BTC';document.getElementById('totalCycles').textContent=intfmt(cy);document.getElementById('poolCards').innerHTML=s.pools.map(p=>`<div class="card"><div class="pool-title"><div class="label"><span class="pool-dot" style="background:${p.color}"></span>${p.label}</div></div><div class="metrics"><div class="metric"><div class="label">Unspent</div><div class="n">${fmt(p.unspent_btc)} BTC</div></div><div class="metric"><div class="label">Entered</div><div class="n">${fmt(p.entered_btc)} BTC</div></div><div class="metric"><div class="label">Cycles</div><div class="n">${intfmt(p.cycles)}</div></div><div class="metric"><div class="label">TX0s</div><div class="n">${intfmt(p.tx0_count)}</div></div><div class="metric"><div class="label">UTXOs</div><div class="n">${intfmt(p.unspent_utxos)}</div></div><div class="metric"><div class="label">Avg fee %</div><div class="n">${fmt(p.avg_fee_efficiency_pct)}%</div></div></div></div>`).join('');document.getElementById('poolToggles').innerHTML='<button class="toggle '+(currentPool==='all'?'active':'')+'" onclick="setPool(\'all\')">All pools</button>'+s.pools.map(p=>`<button class="toggle ${currentPool===p.pool?'active':''}" onclick="setPool('${p.pool}')">${p.label}</button>`).join('')}
function renderCharts(s,c){let poolMap=Object.fromEntries(s.pools.map(p=>[p.pool,p]));s.pools.forEach((p,i)=>{let spent=Math.max((p.entered_btc||0)-(p.unspent_btc||0),0);chart(i===0?'donut0025':'donut0250','doughnut',{labels:['Unspent BTC','Spent/Remixed Out BTC'],datasets:[{data:[p.unspent_btc,spent],backgroundColor:[p.color,'#2c2c2e'],borderColor:'#000',borderWidth:2}]},{cutout:'72%'})});let entered=c.entered||{blocks:[],series:{}};chart('enteredChart','line',{labels:entered.blocks,datasets:Object.keys(entered.series||{}).map(k=>({label:labelFor(k),data:entered.series[k],borderColor:colorFor(k),backgroundColor:'transparent',stepped:true,tension:0,borderWidth:2.2,pointRadius:0}))});let cap=c.capacity||{blocks:[],series:{}};chart('lineChart','line',{labels:cap.blocks,datasets:Object.keys(cap.series||{}).map(k=>({label:labelFor(k),data:cap.series[k],borderColor:colorFor(k),backgroundColor:'transparent',stepped:true,tension:0,borderWidth:2.2,pointRadius:0}))});let u=c.utxos||{blocks:[],total_utxos:[]};chart('utxoChart','line',{labels:u.blocks,datasets:[{label:'Unspent UTXOs',data:u.total_utxos,borderColor:'#f5f5f7',backgroundColor:'rgba(255,255,255,.08)',fill:true,stepped:true,pointRadius:0,borderWidth:2.2}]});chart('poolBreakdown','doughnut',{labels:s.pools.map(p=>p.label),datasets:[{data:s.pools.map(p=>p.unspent_btc),backgroundColor:s.pools.map(p=>p.color),borderColor:'#000',borderWidth:2}]},{cutout:'72%'})}
async function refresh(){let [s,c]=await Promise.all([fetch('/api/summary').then(r=>r.json()),fetch('/api/charts').then(r=>r.json())]);renderSummary(s);renderCharts(s,c);await loadTxs()}
document.getElementById('prevPage').onclick=()=>page(-1);document.getElementById('nextPage').onclick=()=>page(1);refresh();setInterval(refresh,15000);
</script></body></html>
"""
WHIRLPOOL_OBSERVER_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
  <title>Whirlpool.Observer</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root{color-scheme:dark;--bg:#000;--panel:#0b0b0d;--panel2:#111114;--panel3:#17171b;--line:#26262b;--text:#f5f5f7;--muted:#8e8e93;--blue:#0a84ff;--orange:#ff9f0a;--green:#30d158;--red:#ff453a}*{box-sizing:border-box}html,body{margin:0;background:#000;color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",sans-serif}body{min-height:100vh}.wrap{width:min(1480px,100%);margin:0 auto;padding:28px}.hero{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;margin:10px 0 24px}.title{margin:0;font-size:54px;line-height:.96;letter-spacing:-.055em;font-weight:820}.subtitle{margin-top:10px;color:var(--muted);font-size:15px}.grid{display:grid;gap:14px}.cards{grid-template-columns:repeat(4,minmax(0,1fr))}.poolcards{grid-template-columns:repeat(2,minmax(0,1fr));margin-top:14px}.charts{grid-template-columns:repeat(2,minmax(0,1fr));margin-top:14px}.card,.chart,.table{background:var(--panel);border:1px solid var(--line);border-radius:22px;padding:20px;box-shadow:none}.card:nth-child(even),.chart:nth-child(even){background:var(--panel2)}.label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.11em}.value{font-size:32px;font-weight:760;letter-spacing:-.04em;margin-top:8px}.small{font-size:13px;color:var(--muted);margin-top:8px}.progress{height:10px;background:#1c1c1e;border:1px solid var(--line);border-radius:999px;overflow:hidden;margin-top:14px}.bar{height:100%;background:#f5f5f7;border-radius:999px}.chart h2,.table h2{margin:0 0 14px;font-size:20px;letter-spacing:-.03em}.cycle-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.tabs{display:flex;gap:8px;flex-wrap:wrap}.tab,.pager button{border:1px solid var(--line);background:var(--panel3);color:var(--text);border-radius:999px;padding:9px 12px;cursor:pointer}.tab.active{background:#f5f5f7;color:#000;border-color:#f5f5f7}.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}table{width:100%;border-collapse:collapse;min-width:640px}td,th{border-bottom:1px solid var(--line);padding:13px 8px;text-align:left}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}td{color:#e8e8ed}.txlink{font-family:"SF Mono",ui-monospace,monospace;color:#f5f5f7;text-decoration:none;word-break:break-all}.txlink:hover{text-decoration:underline}.pooldot{display:inline-block;width:9px;height:9px;border-radius:999px;margin-right:8px}.pager{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-top:14px;color:var(--muted);font-size:13px}.pager button:disabled{opacity:.35;cursor:not-allowed}canvas{max-width:100%}@media(max-width:980px){.wrap{padding:18px}.hero{display:block}.title{font-size:40px}.cards,.poolcards,.charts{grid-template-columns:1fr}.card,.chart,.table{border-radius:18px;padding:16px}.value{font-size:28px}.cycle-head{display:block}.tabs{margin-top:12px}.chart canvas{max-height:320px}table{min-width:520px}th:nth-child(2),td:nth-child(2){display:none}}@media(max-width:520px){.wrap{padding:12px}.title{font-size:34px}.subtitle{font-size:13px}.cards{gap:10px}.card,.chart,.table{padding:14px}.value{font-size:24px}.chart h2,.table h2{font-size:18px}table{min-width:420px}td,th{padding:11px 6px}.txlink{font-size:12px}.pager{display:grid;grid-template-columns:1fr 1fr}.pager span{grid-column:1/3;text-align:center;order:-1}}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero"><div><h1 class="title">Whirlpool.Observer</h1><div class="subtitle">Pure dark live Whirlpool capacity, TX0, cycle and lineage explorer.</div></div></div>
    <div class="grid cards">
      <div class="card"><div class="label">Sync progress</div><div class="value" id="progressValue">0%</div><div class="progress"><div class="bar" id="progressBar" style="width:0%"></div></div><div class="small" id="heightText"></div></div>
      <div class="card"><div class="label">Total unspent</div><div class="value" id="totalUnspent">0 BTC</div></div>
      <div class="card"><div class="label">Total entered</div><div class="value" id="totalEntered">0 BTC</div></div>
      <div class="card"><div class="label">Total cycles</div><div class="value" id="totalCycles">0</div></div>
    </div>
    <div class="grid poolcards" id="poolCards"></div>
    <div class="grid charts"><div class="chart"><h2>Entered vs Unspent</h2><canvas id="capacityDonut"></canvas></div><div class="chart"><h2>Pool Unspent Breakdown</h2><canvas id="poolDonut"></canvas></div></div>
    <div class="grid charts"><div class="chart"><h2>Total Entered by Pool</h2><canvas id="enteredChart"></canvas></div><div class="chart"><h2>Live Unspent Capacity</h2><canvas id="lineChart"></canvas></div></div>
    <div class="grid charts"><div class="chart"><h2>Total Unspent UTXOs</h2><canvas id="utxoChart"></canvas></div><div class="chart"><h2>Cycle Count by Pool</h2><canvas id="cycleChart"></canvas></div></div>
    <div class="table" style="margin-top:14px"><div class="cycle-head"><h2>Whirlpool Cycles</h2><div class="tabs" id="poolTabs"></div></div><div class="table-scroll"><table><thead><tr><th>Height</th><th>Pool</th><th>TXID</th></tr></thead><tbody id="txRows"></tbody></table></div><div class="pager"><button id="prevPage">Previous</button><span id="pageText">Page 1 / 1</span><button id="nextPage">Next</button></div></div>
  </div>
<script>
let charts={}, summaryCache=null, selectedPool='all', currentPage=1, totalPages=1; const fmt=n=>Number(n||0).toLocaleString(undefined,{maximumFractionDigits:4});
function makeChart(id,type,data,options={}){if(charts[id])charts[id].destroy();charts[id]=new Chart(document.getElementById(id),{type,data,options:{responsive:true,maintainAspectRatio:true,plugins:{legend:{labels:{color:'#f5f5f7',usePointStyle:true}}},scales:type==='doughnut'?{}:{x:{ticks:{color:'#8e8e93',maxTicksLimit:7},grid:{color:'#1c1c1e'}},y:{ticks:{color:'#8e8e93'},grid:{color:'#1c1c1e'}}},...options}})}
function colorForPool(pool){return pool.includes('0.25_')?'#0a84ff':'#ff9f0a'}
function updateTabs(pools){const tabs=document.getElementById('poolTabs');tabs.innerHTML='<button class="tab '+(selectedPool==='all'?'active':'')+'" data-pool="all">All Pools</button>'+pools.map(p=>`<button class="tab ${selectedPool===p.pool?'active':''}" data-pool="${p.pool}">${p.label}</button>`).join('');tabs.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{selectedPool=b.dataset.pool;currentPage=1;refreshTxs();updateTabs(summaryCache.pools)})}
function updateSummary(summary){summaryCache=summary;document.getElementById('progressValue').textContent=summary.progress_pct.toFixed(2)+'%';document.getElementById('progressBar').style.width=summary.progress_pct+'%';document.getElementById('heightText').textContent=`${summary.last_processed_block} / ${summary.tip_height}`;let tu=0,te=0,cy=0;summary.pools.forEach(p=>{tu+=p.unspent_btc;te+=p.entered_btc;cy+=p.cycles});document.getElementById('totalUnspent').textContent=fmt(tu)+' BTC';document.getElementById('totalEntered').textContent=fmt(te)+' BTC';document.getElementById('totalCycles').textContent=cy.toLocaleString();document.getElementById('poolCards').innerHTML=summary.pools.map(p=>`<div class="card"><div class="label">${p.label}</div><div class="value">${fmt(p.unspent_btc)} BTC</div><div class="small">Entered: ${fmt(p.entered_btc)} BTC · TX0s: ${(p.tx0_count||0).toLocaleString()} · Cycles: ${(p.cycles||0).toLocaleString()}</div></div>`).join('');updateTabs(summary.pools);makeChart('capacityDonut','doughnut',{labels:['Entered BTC','Unspent BTC'],datasets:[{data:[te,tu],backgroundColor:['#3a3a3c','#f5f5f7'],borderColor:'#000'}]},{cutout:'70%'});makeChart('poolDonut','doughnut',{labels:summary.pools.map(p=>p.label),datasets:[{data:summary.pools.map(p=>p.unspent_btc),backgroundColor:summary.pools.map(p=>p.color),borderColor:'#000'}]},{cutout:'70%'});makeChart('cycleChart','doughnut',{labels:summary.pools.map(p=>p.label),datasets:[{data:summary.pools.map(p=>p.cycles),backgroundColor:summary.pools.map(p=>p.color),borderColor:'#000'}]},{cutout:'70%'})}
function updateCharts(chartsData){const cap=chartsData.capacity;makeChart('lineChart','line',{labels:cap.blocks,datasets:Object.keys(cap.series||{}).map(k=>({label:k.replace('_BTC_Pool',' BTC'),data:cap.series[k],borderColor:colorForPool(k),backgroundColor:'transparent',stepped:true,tension:0,borderWidth:2,pointRadius:0}))});const entered=chartsData.entered;makeChart('enteredChart','line',{labels:entered.blocks,datasets:Object.keys(entered.series||{}).map(k=>({label:k.replace('_BTC_Pool',' BTC'),data:entered.series[k],borderColor:colorForPool(k),backgroundColor:'transparent',stepped:true,tension:0,borderWidth:2,pointRadius:0}))});const u=chartsData.utxos;makeChart('utxoChart','line',{labels:u.blocks,datasets:[{label:'Unspent UTXOs',data:u.total_utxos,borderColor:'#f5f5f7',backgroundColor:'transparent',stepped:true,pointRadius:0,borderWidth:2}]})}
async function refreshTxs(){const poolParam=selectedPool==='all'?'':`&pool=${encodeURIComponent(selectedPool)}`;const data=await fetch(`/api/txs?page=${currentPage}&per_page=25${poolParam}`).then(r=>r.json());totalPages=data.total_pages||1;document.getElementById('pageText').textContent=`Page ${data.page} / ${totalPages} · ${data.total} cycles`;document.getElementById('prevPage').disabled=data.page<=1;document.getElementById('nextPage').disabled=data.page>=totalPages;document.getElementById('txRows').innerHTML=(data.items||[]).map(t=>`<tr><td>${t.block_height}</td><td><span class="pooldot" style="background:${t.pool_color}"></span>${t.pool_label}</td><td><a class="txlink" target="_blank" href="${t.am_i_exposed_url}">${t.txid}</a></td></tr>`).join('')||'<tr><td colspan="3">No cycles found yet.</td></tr>'}
async function refresh(){const [summary,chartsData]=await Promise.all([fetch('/api/summary').then(r=>r.json()),fetch('/api/charts').then(r=>r.json())]);updateSummary(summary);updateCharts(chartsData);await refreshTxs()}
document.getElementById('prevPage').onclick=()=>{if(currentPage>1){currentPage--;refreshTxs()}};document.getElementById('nextPage').onclick=()=>{if(currentPage<totalPages){currentPage++;refreshTxs()}};refresh();setInterval(refresh,15000);
</script></body></html>
"""


WHIRLPOOL_OBSERVER_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,viewport-fit=cover">
  <title>Whirlpool.Observer</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root{color-scheme:dark;--bg:#000;--panel:#090909;--line:#262626;--text:#f5f5f7;--muted:#8e8e93;--white:#f5f5f7;--grey:#8e8e93}*{box-sizing:border-box}html,body{margin:0;background:#000;color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",system-ui,sans-serif}body{min-height:100vh}.wrap{width:min(1480px,100%);margin:0 auto;padding:28px}.hero{margin:10px 0 24px;text-align:center}.title{margin:0;font-size:54px;line-height:.96;letter-spacing:-.055em;font-weight:820}.subtitle{margin-top:10px;color:var(--muted);font-size:15px}.grid{display:grid;gap:14px}.cards{grid-template-columns:repeat(4,minmax(0,1fr))}.poolcards{grid-template-columns:repeat(2,minmax(0,1fr));margin-top:14px}.charts{grid-template-columns:repeat(2,minmax(0,1fr));margin-top:14px}.card,.chart,.table{background:var(--panel);border:1px solid var(--line);border-radius:22px;padding:20px;box-shadow:none}.card,.metric{text-align:center}.label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.11em}.value{font-size:32px;font-weight:760;letter-spacing:-.04em;margin-top:8px;overflow-wrap:anywhere}.small{font-size:13px;color:var(--muted);margin-top:8px}.progress{height:10px;background:#1a1a1a;border:1px solid var(--line);border-radius:999px;overflow:hidden;margin-top:14px}.bar{height:100%;background:var(--white);border-radius:999px}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px}.metric{background:#000;border:1px solid var(--line);border-radius:16px;padding:12px}.metric .n{font-weight:740;font-size:20px;margin-top:4px}.chart h2,.table h2{margin:0 0 14px;font-size:20px;letter-spacing:-.03em;text-align:center}.chart canvas{max-height:390px}.cycle-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.tabs{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.tab,.pager button{border:1px solid var(--line);background:#000;color:var(--text);border-radius:999px;padding:9px 12px;cursor:pointer}.tab.active{background:var(--white);color:#000;border-color:var(--white)}.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid var(--line);padding:12px 8px;text-align:left;color:#e5e5ea;vertical-align:top}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}.mono,.txlink{font-family:"SF Mono",ui-monospace,monospace}.txlink{color:var(--white);text-decoration:none;overflow-wrap:anywhere}.txlink:hover{text-decoration:underline}.pooldot{display:inline-block;width:10px;height:10px;border-radius:999px;margin-right:8px}.tx0-list{font-size:12px;color:var(--muted);line-height:1.55}.pager{display:flex;gap:10px;align-items:center;justify-content:center;margin-top:14px;color:var(--muted)}.mobile-cards{display:none}.mobile-cycle-card{background:#000;border:1px solid var(--line);border-radius:16px;padding:12px;margin-top:10px}.empty{color:var(--muted);text-align:center}.desktop-only{display:table-cell}@media(max-width:980px){.wrap{padding:16px}.title{font-size:40px}.cards,.poolcards,.charts{grid-template-columns:1fr}.metrics{grid-template-columns:1fr 1fr}.cycle-head{display:block}.tabs{justify-content:flex-start;margin-top:12px}.desktop-table{display:none}.mobile-cards{display:block}.desktop-only{display:none}.card,.chart,.table{border-radius:18px;padding:16px}.value{font-size:28px}.chart canvas{max-height:320px}}@media(max-width:420px){.metrics{grid-template-columns:1fr}.title{font-size:34px}.wrap{padding:12px}}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero"><h1 class="title">Whirlpool.Observer</h1><div class="subtitle">Pure-black live Whirlpool capacity, TX0, cycle and lineage explorer.</div></div>
    <div class="grid cards">
      <div class="card"><div class="label" id="syncLabel">Sync progress</div><div class="value" id="progressValue">0%</div><div class="progress" id="progressWrap"><div class="bar" id="progressBar" style="width:0%"></div></div><div class="small" id="heightText"></div></div>
      <div class="card"><div class="label">Total unspent</div><div class="value" id="totalUnspent">0 BTC</div></div>
      <div class="card"><div class="label">Total entered</div><div class="value" id="totalEntered">0 BTC</div></div>
      <div class="card"><div class="label">Total cycles</div><div class="value" id="totalCycles">0</div></div>
    </div>
    <div class="grid poolcards" id="poolCards"></div>
    <div class="grid charts"><div class="chart"><h2>Total Entered Capacity by Pool</h2><canvas id="enteredChart"></canvas></div><div class="chart"><h2>Live Unspent Capacity by Pool</h2><canvas id="lineChart"></canvas></div></div>
    <div class="grid charts"><div class="chart"><h2>Total Unspent UTXOs</h2><canvas id="utxoChart"></canvas></div><div class="chart"><h2>Cycle Count by Blockheight</h2><canvas id="cycleChart"></canvas></div></div>
    <div class="table" style="margin-top:14px"><div class="cycle-head"><h2>Whirlpool Cycles</h2><div class="tabs" id="poolTabs"></div></div><div class="table-scroll desktop-table"><table><thead><tr><th>Height</th><th>Pool</th><th>TXID</th><th class="desktop-only">Input TX0s / Fee efficiency</th></tr></thead><tbody id="txRows"></tbody></table></div><div class="mobile-cards" id="txCards"></div><div class="pager"><button id="prevPage">Previous</button><span id="pageText">Page 1 / 1</span><button id="nextPage">Next</button></div></div>
  </div>
<script>
let charts={},summaryCache=null,selectedPool='all',currentPage=1,totalPages=1;const fmt=n=>Number(n||0).toLocaleString(undefined,{maximumFractionDigits:4});const intfmt=n=>Number(n||0).toLocaleString();const hms=s=>{if(s==null)return '';s=Math.max(0,Number(s)||0);let h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=Math.floor(s%60);return h?`${h}h ${m}m`:m?`${m}m ${sec}s`:`${sec}s`};
function colorForPool(pool){return pool.includes('0.25_')?'#f5f5f7':'#8e8e93'}
function labelFor(pool){let p=(summaryCache?.pools||[]).find(x=>x.pool===pool);return p?p.label:pool}
function makeChart(id,type,data,options={}){if(charts[id])charts[id].destroy();charts[id]=new Chart(document.getElementById(id),{type,data,options:{responsive:true,maintainAspectRatio:true,plugins:{legend:{labels:{color:'#f5f5f7',usePointStyle:true}},tooltip:{mode:'index',intersect:false}},scales:{x:{ticks:{color:'#8e8e93',maxTicksLimit:7},grid:{color:'#1c1c1e'}},y:{ticks:{color:'#8e8e93'},grid:{color:'#1c1c1e'}}},...options}})}
function updateTabs(pools){const tabs=document.getElementById('poolTabs');tabs.innerHTML='<button class="tab '+(selectedPool==='all'?'active':'')+'" data-pool="all">All Pools</button>'+pools.map(p=>`<button class="tab ${selectedPool===p.pool?'active':''}" data-pool="${p.pool}">${p.label}</button>`).join('');tabs.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{selectedPool=b.dataset.pool;currentPage=1;refreshTxs();updateTabs(summaryCache.pools)})}
function updateSummary(s){summaryCache=s;let tu=0,te=0,cy=0;s.pools.forEach(p=>{tu+=p.unspent_btc;te+=p.entered_btc;cy+=p.cycles});if(s.is_synced){document.getElementById('syncLabel').textContent='Synced';document.getElementById('progressValue').textContent=`Blockheight ${intfmt(s.last_processed_block)}`;document.getElementById('progressWrap').style.display='none';document.getElementById('heightText').textContent=s.next_update_seconds==null?'Waiting for next update':`Next update in ${hms(s.next_update_seconds)}`}else{document.getElementById('syncLabel').textContent='Sync progress';document.getElementById('progressValue').textContent=s.progress_pct.toFixed(2)+'%';document.getElementById('progressWrap').style.display='block';document.getElementById('progressBar').style.width=s.progress_pct+'%';document.getElementById('heightText').textContent=`${intfmt(s.last_processed_block)} / ${intfmt(s.tip_height)}`}document.getElementById('totalUnspent').textContent=fmt(tu)+' BTC';document.getElementById('totalEntered').textContent=fmt(te)+' BTC';document.getElementById('totalCycles').textContent=intfmt(cy);document.getElementById('poolCards').innerHTML=s.pools.map(p=>`<div class="card"><div class="label">${p.label}</div><div class="value">${fmt(p.unspent_btc)} BTC</div><div class="metrics"><div class="metric"><div class="label">Entered</div><div class="n">${fmt(p.entered_btc)} BTC</div></div><div class="metric"><div class="label">Cycles</div><div class="n">${intfmt(p.cycles)}</div></div><div class="metric"><div class="label">TX0s</div><div class="n">${intfmt(p.tx0_count)}</div></div><div class="metric"><div class="label">UTXOs</div><div class="n">${intfmt(p.unspent_utxos)}</div></div><div class="metric"><div class="label">Avg fee %</div><div class="n">${fmt(p.avg_fee_efficiency_pct)}%</div></div><div class="metric"><div class="label">Pool color</div><div class="n"><span class="pooldot" style="background:${p.color}"></span></div></div></div></div>`).join('');updateTabs(s.pools)}
function cycleSeriesByPool(capacity){const blocks=capacity.blocks||[],series=capacity.series||{};return Object.keys(series).map(pool=>({label:labelFor(pool),borderColor:colorForPool(pool),backgroundColor:'transparent',stepped:true,tension:0,borderWidth:2,pointRadius:0,data:blocks.map((_,i)=>i+1)}))}
function updateCharts(c){const entered=c.entered||{blocks:[],series:{}};makeChart('enteredChart','line',{labels:entered.blocks,datasets:Object.keys(entered.series||{}).map(k=>({label:labelFor(k),data:entered.series[k],borderColor:colorForPool(k),backgroundColor:'transparent',stepped:true,tension:0,borderWidth:2,pointRadius:0}))});const cap=c.capacity||{blocks:[],series:{}};makeChart('lineChart','line',{labels:cap.blocks,datasets:Object.keys(cap.series||{}).map(k=>({label:labelFor(k),data:cap.series[k],borderColor:colorForPool(k),backgroundColor:'transparent',stepped:true,tension:0,borderWidth:2,pointRadius:0}))});const u=c.utxos||{blocks:[],total_utxos:[]};makeChart('utxoChart','line',{labels:u.blocks,datasets:[{label:'Unspent UTXOs',data:u.total_utxos,borderColor:'#f5f5f7',backgroundColor:'transparent',stepped:true,pointRadius:0,borderWidth:2}]});makeChart('cycleChart','line',{labels:cap.blocks,datasets:cycleSeriesByPool(cap)})}
function tx0Text(t){return(t.tx0_inputs||[]).length?t.tx0_inputs.map(x=>`<div>${x.txid.slice(0,12)}… · ${x.fee_efficiency_pct}%</div>`).join(''):'<span class="empty">None recorded</span>'}
async function refreshTxs(){const poolParam=selectedPool==='all'?'':`&pool=${encodeURIComponent(selectedPool)}`;const data=await fetch(`/api/txs?page=${currentPage}&per_page=25${poolParam}`).then(r=>r.json());totalPages=data.total_pages||1;document.getElementById('pageText').textContent=`Page ${data.page} / ${totalPages} · ${data.total} cycles`;document.getElementById('prevPage').disabled=data.page<=1;document.getElementById('nextPage').disabled=data.page>=totalPages;let rows=data.items||[];document.getElementById('txRows').innerHTML=rows.length?rows.map(t=>`<tr><td>${intfmt(t.block_height)}</td><td><span class="pooldot" style="background:${t.pool_color}"></span>${t.pool_label}</td><td><a class="txlink" target="_blank" href="${t.am_i_exposed_url}">${t.txid}</a></td><td class="desktop-only tx0-list">${tx0Text(t)}</td></tr>`).join(''):'<tr><td colspan="4" class="empty">No cycles found yet.</td></tr>';document.getElementById('txCards').innerHTML=rows.length?rows.map(t=>`<div class="mobile-cycle-card"><div>${intfmt(t.block_height)} · <span class="pooldot" style="background:${t.pool_color}"></span>${t.pool_label}</div><a class="txlink" target="_blank" href="${t.am_i_exposed_url}">${t.txid}</a></div>`).join(''):'<div class="mobile-cycle-card empty">No cycles found yet.</div>'}
async function refresh(){const[s,c]=await Promise.all([fetch('/api/summary').then(r=>r.json()),fetch('/api/charts').then(r=>r.json())]);updateSummary(s);updateCharts(c);await refreshTxs()}
document.getElementById('prevPage').onclick=()=>{if(currentPage>1){currentPage--;refreshTxs()}};document.getElementById('nextPage').onclick=()=>{if(currentPage<totalPages){currentPage++;refreshTxs()}};refresh();setInterval(refresh,15000);
</script></body></html>
"""

def _handle_shutdown_signal(signum, _frame):
    logging.info(f"Received signal {signum}; graceful shutdown requested.")
    STOP_EVENT.set()


def main():
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    parser = argparse.ArgumentParser(description="Whirlpool.Observer: Docker-first Whirlpool lineage scanner and live web explorer.")
    parser.add_argument("command", choices=["run", "stats", "report", "simplereport"], help="run scanner/web UI, view stats, or generate reports")
    parser.add_argument("--fresh", action="store_true", help="Start with a fresh, empty database. Deletes existing data.")
    parser.add_argument("--interval", type=int, help="The block interval for reports.")
    parser.add_argument("--no-web", action="store_true", help="Run scanner without the web UI.")
    args = parser.parse_args()

    if args.command == "run":
        tracer = WhirlpoolTracer(fresh_start=args.fresh)
        tracer.run(with_web=not args.no_web)
    elif args.command == "stats":
        tracer = WhirlpoolTracer()
        tracer.display_stats()
        tracer.db_manager.close()
    elif args.command == "report":
        tracer = WhirlpoolTracer()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tracer.generate_report(interval=args.interval or 1000, output_file=os.path.join(REPORTS_DIR, f"whirlpool_report_{timestamp}.csv"))
        tracer.db_manager.close()
    elif args.command == "simplereport":
        tracer = WhirlpoolTracer()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tracer.generate_simple_report(interval=args.interval or 10, output_file=os.path.join(REPORTS_DIR, f"whirlpool_simplereport_{timestamp}.csv"))
        tracer.db_manager.close()


if __name__ == "__main__":
    main()
