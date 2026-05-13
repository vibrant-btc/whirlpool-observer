import argparse
import csv
import logging
import os
import signal
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from bitcoin.core import CBlock

# --- Constants ---
DATA_DIR = os.environ.get("WHIRLPOOL_DATA_DIR", ".")
REPORTS_DIR = os.environ.get("WHIRLPOOL_REPORTS_DIR", ".")
DB_FILE = os.environ.get("WHIRLPOOL_DB_FILE", os.path.join(DATA_DIR, "whirlpool.db"))
MEMPOOL_API_BASE_URL = os.environ.get("MEMPOOL_API_URL", "https://mempool.space/api").rstrip("/")
FALLBACK_API_BASE_URL = os.environ.get("MEMPOOL_FALLBACK_API_URL", "https://blockstream.info/api").rstrip("/")
OUTSPENDS_PRIMARY_URL = "https://mempool.space/api"
OUTSPENDS_FALLBACK_URL = "https://blockstream.info/api"
WEB_HOST = os.environ.get("WHIRLPOOL_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WHIRLPOOL_WEB_PORT", "8080"))
ONION_LOCATION = "__WHIRLPOOL_ONION_LOCATION__".strip()
if ONION_LOCATION.startswith("__"):
    ONION_LOCATION = ""
RESCAN_INTERVAL_HOURS = float(os.environ.get("WHIRLPOOL_RESCAN_HOURS", "12"))
PROCESS_LOOP_DELAY_SECONDS = max(int(RESCAN_INTERVAL_HOURS * 60 * 60), 60)
RETRY_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 5
API_REQUEST_DELAY_SECONDS = float(os.environ.get("WHIRLPOOL_API_REQUEST_DELAY_SECONDS", "0.5"))
RETRY_WAIT_AFTER_ALL_FAILURES_SECONDS = 300
SATOSHIS_PER_BTC = 100_000_000
WHIRLPOOL_TX_INPUTS = 5
WHIRLPOOL_TX_OUTPUTS = 5
MAX_TX0_PREMIX_OUTPUTS = 20
TX0_PREMIX_EXTRA_SATS_MAX = int(os.environ.get("TX0_PREMIX_EXTRA_SATS_MAX", "100000"))
NO_SPEND_UNTIL_BLOCK = 899335

TX0_STATUS_UNMIXED = "unmixed"
TX0_STATUS_MIXED = "mixed"
TX0_STATUS_EXITED = "exited"
TX0_STATUS_REMOVED = "removed"

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

class ColoredFormatter(logging.Formatter):
    COLORS = {
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    RESET = "\033[0m"

    def format(self, record):
        message = super().format(record)
        color = self.COLORS.get(record.levelno)
        return f"{color}{message}{self.RESET}" if color else message


_handler = logging.StreamHandler()
_handler.setFormatter(ColoredFormatter("%(asctime)s - %(levelname)s - %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
logging.getLogger("matplotlib").setLevel(logging.WARNING)
logging.getLogger("fontTools").setLevel(logging.WARNING)
STOP_EVENT = threading.Event()


def btc(sats: int) -> float:
    return (sats or 0) / SATOSHIS_PER_BTC


def human_duration(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def txid_from_core(tx) -> str:
    return tx.GetTxid()[::-1].hex()


def prev_txid_from_vin(vin) -> str:
    return vin.prevout.hash[::-1].hex()


def is_zero_sat_op_return(vout) -> bool:
    return int(vout.nValue) == 0 and bytes(vout.scriptPubKey).startswith(b"\x6a")


def is_json_zero_sat_op_return(vout: Dict[str, Any]) -> bool:
    if int(vout.get("value", 0) or 0) != 0:
        return False
    script_type = str(vout.get("scriptpubkey_type", "")).lower()
    asm = str(vout.get("scriptpubkey_asm", "")).upper()
    script = str(vout.get("scriptpubkey", "")).lower()
    return script_type == "op_return" or asm.startswith("OP_RETURN") or script.startswith("6a")


class DatabaseManager:
    """Thread-safe SQLite storage for Whirlpool cycles, TX0s, premix-output states, and UI statistics."""

    def __init__(self, db_file: str):
        self.db_file = db_file
        os.makedirs(os.path.dirname(os.path.abspath(self.db_file)), exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_file, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()

    def _drop_tables(self):
        for table in [
            "progress",
            "whirlpool_txs",
            "anonymity_set_utxos",
            "tx_inputs",
            "tx0s",
            "tx0_premix_outputs",
        ]:
            self.cursor.execute(f"DROP TABLE IF EXISTS {table}")

    def setup_db(self, start_block_height: int, fresh_start: bool = False):
        logging.info("Setting up Whirlpool.Observer database.")
        with self.lock:
            if fresh_start:
                logging.warning("Fresh start requested. Dropping existing Whirlpool.Observer tables.")
                self._drop_tables()

            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS progress (
                    key TEXT PRIMARY KEY,
                    value INTEGER
                )
            """)
            self.cursor.execute(
                "INSERT OR IGNORE INTO progress (key, value) VALUES ('last_processed_block_height', ?)",
                (start_block_height - 1,),
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
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_utxos_pool ON anonymity_set_utxos(pool_name)")

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
                    premix_value_sats INTEGER,
                    premix_fee_extra_sats INTEGER,
                    poolsize_sats INTEGER DEFAULT 0,
                    pool_utxo_count INTEGER DEFAULT 0,
                    unmixed_count INTEGER DEFAULT 0,
                    mixed_count INTEGER DEFAULT 0,
                    exited_count INTEGER DEFAULT 0,
                    removed_count INTEGER DEFAULT 0,
                    fee_paid_pct REAL,
                    first_seen_whirlpool_txid TEXT,
                    observed_at_block INTEGER,
                    last_status_refresh_block INTEGER
                )
            """)
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx0s_pool ON tx0s(pool_name)")
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx0s_observed ON tx0s(observed_at_block)")
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx0s_unmixed ON tx0s(unmixed_count)")

            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS tx0_premix_outputs (
                    output_id TEXT PRIMARY KEY,
                    tx0_txid TEXT,
                    vout INTEGER,
                    value_sats INTEGER,
                    denomination_sats INTEGER,
                    fee_extra_sats INTEGER,
                    pool_name TEXT,
                    status TEXT,
                    status_reason TEXT,
                    spent_txid TEXT,
                    spent_vin INTEGER,
                    spent_block_height INTEGER,
                    spent_block_hash TEXT,
                    spent_tx_is_whirlpool BOOLEAN DEFAULT 0,
                    spent_tx_pool_name TEXT,
                    spent_in_whirlpool_txid TEXT,
                    spent_input_index INTEGER
                )
            """)
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx0_outputs_tx0 ON tx0_premix_outputs(tx0_txid)")
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx0_outputs_pool ON tx0_premix_outputs(pool_name)")
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx0_outputs_status ON tx0_premix_outputs(status)")
            self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx0_outputs_spent ON tx0_premix_outputs(spent_txid)")
            self.conn.commit()
        logging.info("Database setup complete.")

    def get_progress(self, key: str) -> Optional[int]:
        with self.lock:
            self.cursor.execute("SELECT value FROM progress WHERE key=?", (key,))
            row = self.cursor.fetchone()
            return int(row["value"]) if row else None

    def update_progress(self, key: str, value: int):
        with self.lock, self.conn:
            self.cursor.execute("REPLACE INTO progress (key, value) VALUES (?, ?)", (key, value))

    def is_db_seeded(self) -> bool:
        with self.lock:
            for genesis_info in GENESIS_TXS.values():
                self.cursor.execute("SELECT 1 FROM whirlpool_txs WHERE txid=?", (genesis_info["txid"],))
                if self.cursor.fetchone() is None:
                    return False
            return True

    def add_whirlpool_tx(self, tx_data: Dict[str, Any]):
        with self.lock, self.conn:
            self.cursor.execute("""
                INSERT OR IGNORE INTO whirlpool_txs (txid, block_height, block_hash, pool_name)
                VALUES (:txid, :block_height, :block_hash, :pool_name)
            """, tx_data)

    def add_whirlpool_tx_with_utxos(self, tx_data: Dict[str, Any], utxos: List[Dict[str, Any]]):
        with self.lock, self.conn:
            self.cursor.execute("""
                INSERT OR IGNORE INTO whirlpool_txs (txid, block_height, block_hash, pool_name)
                VALUES (:txid, :block_height, :block_hash, :pool_name)
            """, tx_data)
            records = [(f"{u['txid']}:{u['vout']}", u["txid"], u["vout"], u["value_sats"], u["pool_name"]) for u in utxos]
            self.cursor.executemany("""
                INSERT OR IGNORE INTO anonymity_set_utxos (output_id, txid, vout, value_sats, pool_name)
                VALUES (?, ?, ?, ?, ?)
            """, records)

    def get_unspent_utxo_by_id(self, output_id: str) -> Optional[sqlite3.Row]:
        with self.lock:
            self.cursor.execute("SELECT * FROM anonymity_set_utxos WHERE output_id=? AND is_spent=0", (output_id,))
            return self.cursor.fetchone()

    def get_any_utxo_by_id(self, output_id: str) -> Optional[sqlite3.Row]:
        with self.lock:
            self.cursor.execute("SELECT * FROM anonymity_set_utxos WHERE output_id=?", (output_id,))
            return self.cursor.fetchone()

    def mark_utxo_as_spent(self, output_id: str, spent_in_txid: str, spent_in_block_height: int):
        with self.lock, self.conn:
            self.cursor.execute("""
                UPDATE anonymity_set_utxos
                SET is_spent=1, spent_in_txid=?, spent_in_block_height=?
                WHERE output_id=?
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
                 premix_value_sats, premix_fee_extra_sats, poolsize_sats, pool_utxo_count, unmixed_count,
                 mixed_count, exited_count, removed_count, fee_paid_pct, first_seen_whirlpool_txid,
                 observed_at_block, last_status_refresh_block)
                VALUES (:txid, :pool_name, :block_height, :block_hash, :coordinator_fee_sats, :premix_output_count,
                        :premix_value_sats, :premix_fee_extra_sats, 0, 0, 0, 0, 0, 0, :fee_paid_pct,
                        :first_seen_whirlpool_txid, :observed_at_block, :last_status_refresh_block)
                ON CONFLICT(txid) DO UPDATE SET
                    pool_name=excluded.pool_name,
                    block_height=COALESCE(excluded.block_height, tx0s.block_height),
                    block_hash=COALESCE(excluded.block_hash, tx0s.block_hash),
                    coordinator_fee_sats=excluded.coordinator_fee_sats,
                    premix_output_count=excluded.premix_output_count,
                    premix_value_sats=excluded.premix_value_sats,
                    premix_fee_extra_sats=excluded.premix_fee_extra_sats,
                    fee_paid_pct=excluded.fee_paid_pct,
                    first_seen_whirlpool_txid=COALESCE(tx0s.first_seen_whirlpool_txid, excluded.first_seen_whirlpool_txid),
                    observed_at_block=COALESCE(tx0s.observed_at_block, excluded.observed_at_block)
            """, tx0)
            for output in premix_outputs:
                self.cursor.execute("""
                    INSERT INTO tx0_premix_outputs
                    (output_id, tx0_txid, vout, value_sats, denomination_sats, fee_extra_sats, pool_name,
                     status, status_reason, spent_txid, spent_vin, spent_block_height, spent_block_hash,
                     spent_tx_is_whirlpool, spent_tx_pool_name, spent_in_whirlpool_txid, spent_input_index)
                    VALUES (:output_id, :tx0_txid, :vout, :value_sats, :denomination_sats, :fee_extra_sats, :pool_name,
                            :status, :status_reason, :spent_txid, :spent_vin, :spent_block_height, :spent_block_hash,
                            :spent_tx_is_whirlpool, :spent_tx_pool_name, :spent_in_whirlpool_txid, :spent_input_index)
                    ON CONFLICT(output_id) DO UPDATE SET
                        pool_name=excluded.pool_name,
                        value_sats=excluded.value_sats,
                        denomination_sats=excluded.denomination_sats,
                        fee_extra_sats=excluded.fee_extra_sats,
                        spent_in_whirlpool_txid=COALESCE(tx0_premix_outputs.spent_in_whirlpool_txid, excluded.spent_in_whirlpool_txid),
                        spent_input_index=COALESCE(tx0_premix_outputs.spent_input_index, excluded.spent_input_index)
                """, output)
            self._refresh_all_tx0_aggregates_locked()

    def update_tx0_output_status(self, output_id: str, status: str, reason: str, spent_txid: Optional[str], spent_vin: Optional[int],
                                 spent_block_height: Optional[int], spent_block_hash: Optional[str], spent_tx_is_whirlpool: bool,
                                 spent_tx_pool_name: Optional[str], spent_in_whirlpool_txid: Optional[str], spent_input_index: Optional[int]):
        with self.lock, self.conn:
            self.cursor.execute("""
                UPDATE tx0_premix_outputs
                SET status=?, status_reason=?, spent_txid=?, spent_vin=?, spent_block_height=?, spent_block_hash=?,
                    spent_tx_is_whirlpool=?, spent_tx_pool_name=?, spent_in_whirlpool_txid=?, spent_input_index=?
                WHERE output_id=?
            """, (status, reason, spent_txid, spent_vin, spent_block_height, spent_block_hash, 1 if spent_tx_is_whirlpool else 0,
                  spent_tx_pool_name, spent_in_whirlpool_txid, spent_input_index, output_id))
            tx0_row = self.query_one_locked("SELECT tx0_txid FROM tx0_premix_outputs WHERE output_id=?", (output_id,))
            if tx0_row:
                self._refresh_tx0_aggregate_locked(tx0_row["tx0_txid"])

    def mark_tx0_last_refresh(self, tx0_txid: str, block_height: int):
        with self.lock, self.conn:
            self.cursor.execute("UPDATE tx0s SET last_status_refresh_block=? WHERE txid=?", (block_height, tx0_txid))
            self._refresh_tx0_aggregate_locked(tx0_txid)

    def _refresh_all_tx0_aggregates_locked(self):
        self.cursor.execute("SELECT txid FROM tx0s")
        for row in self.cursor.fetchall():
            self._refresh_tx0_aggregate_locked(row["txid"])

    def refresh_all_tx0_aggregates(self):
        with self.lock, self.conn:
            self._refresh_all_tx0_aggregates_locked()

    def _refresh_tx0_aggregate_locked(self, tx0_txid: str):
        self.cursor.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN status='unmixed' THEN denomination_sats ELSE 0 END),0) AS poolsize_sats,
                COALESCE(SUM(CASE WHEN status='unmixed' THEN 1 ELSE 0 END),0) AS pool_utxo_count,
                COALESCE(SUM(CASE WHEN status='unmixed' THEN 1 ELSE 0 END),0) AS unmixed_count,
                COALESCE(SUM(CASE WHEN status='mixed' THEN 1 ELSE 0 END),0) AS mixed_count,
                COALESCE(SUM(CASE WHEN status='exited' THEN 1 ELSE 0 END),0) AS exited_count,
                COALESCE(SUM(CASE WHEN status='removed' THEN 1 ELSE 0 END),0) AS removed_count
            FROM tx0_premix_outputs WHERE tx0_txid=?
        """, (tx0_txid,))
        row = self.cursor.fetchone()
        if row:
            self.cursor.execute("""
                UPDATE tx0s
                SET poolsize_sats=?, pool_utxo_count=?, unmixed_count=?, mixed_count=?, exited_count=?, removed_count=?
                WHERE txid=?
            """, (row["poolsize_sats"], row["pool_utxo_count"], row["unmixed_count"], row["mixed_count"], row["exited_count"], row["removed_count"], tx0_txid))

    def get_whirlpool_txs_missing_inputs(self, limit: int = 2000) -> List[sqlite3.Row]:
        with self.lock:
            self.cursor.execute("""
                SELECT w.*, COUNT(DISTINCT i.input_index) AS input_count,
                       COUNT(DISTINCT u.vout) AS output_count,
                       SUM(CASE WHEN i.source_type IS NULL OR i.source_type='unknown' THEN 1 ELSE 0 END) AS unknown_input_count
                FROM whirlpool_txs w
                LEFT JOIN tx_inputs i ON i.whirlpool_txid=w.txid
                LEFT JOIN anonymity_set_utxos u ON u.txid=w.txid
                GROUP BY w.txid
                HAVING input_count != 5 OR output_count != 5 OR unknown_input_count > 0
                ORDER BY w.block_height ASC
                LIMIT ?
            """, (limit,))
            return self.cursor.fetchall()

    def get_unmixed_tx0_outputs(self, limit: int = 500) -> List[sqlite3.Row]:
        with self.lock:
            self.cursor.execute("""
                SELECT * FROM tx0_premix_outputs
                WHERE status='unmixed'
                ORDER BY tx0_txid ASC, vout ASC
                LIMIT ?
            """, (limit,))
            return self.cursor.fetchall()

    def get_anonymity_set_stats(self) -> Dict[str, Any]:
        with self.lock:
            self.cursor.execute("""
                SELECT pool_name, COUNT(*) AS count, COALESCE(SUM(value_sats),0) AS total_sats
                FROM anonymity_set_utxos
                WHERE is_spent=0
                GROUP BY pool_name
            """)
            return {row["pool_name"]: {"count": row["count"], "total_sats": row["total_sats"] or 0} for row in self.cursor.fetchall()}

    def query_all(self, sql: str, params: Tuple = ()) -> List[sqlite3.Row]:
        with self.lock:
            self.cursor.execute(sql, params)
            return self.cursor.fetchall()

    def query_one(self, sql: str, params: Tuple = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.query_one_locked(sql, params)

    def query_one_locked(self, sql: str, params: Tuple = ()) -> Optional[sqlite3.Row]:
        self.cursor.execute(sql, params)
        return self.cursor.fetchone()

    def close(self):
        with self.lock:
            self.conn.close()


class MempoolClient:
    """Primary/fallback Esplora-compatible HTTP client, plus hardcoded outspends endpoints."""

    def __init__(self):
        self.base_urls = []
        for url in [MEMPOOL_API_BASE_URL, FALLBACK_API_BASE_URL]:
            cleaned = self._clean_esplora_base_url(url)
            if cleaned and cleaned not in self.base_urls:
                self.base_urls.append(cleaned)
        self.outspends_urls = [self._clean_esplora_base_url(OUTSPENDS_PRIMARY_URL), self._clean_esplora_base_url(OUTSPENDS_FALLBACK_URL)]
        self.request_lock = threading.Lock()
        self.last_request_at = 0.0
        self.tx_cache: Dict[str, Dict[str, Any]] = {}
        self.outspends_cache: Dict[str, List[Dict[str, Any]]] = {}
        if not self.base_urls:
            raise SystemExit("No blockchain API base URL configured.")
        logging.info(f"Primary blockchain API: {self.base_urls[0]}")
        if len(self.base_urls) > 1:
            logging.info(f"Fallback blockchain API: {self.base_urls[1]}")
        logging.info(f"TX0 outspends API order: {self.outspends_urls[0]} then {self.outspends_urls[1]}")

    def _clean_esplora_base_url(self, base_url: str) -> str:
        cleaned = (base_url or "").rstrip("/")
        if not cleaned:
            return ""
        if cleaned.endswith("/api/v1"):
            cleaned = cleaned[:-3]
        elif not cleaned.endswith("/api"):
            cleaned = f"{cleaned}/api"
        return cleaned

    def _request_once(self, base_url: str, endpoint: str, is_json: bool) -> Optional[Any]:
        url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        with self.request_lock:
            elapsed = time.time() - self.last_request_at
            wait_seconds = max(API_REQUEST_DELAY_SECONDS - elapsed, 0)
            if wait_seconds:
                logging.info(f"Waiting {wait_seconds:.2f}s before next REST API call.")
                if STOP_EVENT.wait(wait_seconds):
                    raise SystemExit("Shutdown requested while rate-limiting API calls.")
            try:
                logging.info(f"API request: GET {url}")
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                return response.json() if is_json else response
            except requests.exceptions.RequestException as e:
                logging.warning(f"API request failed: GET {url} -> {e}")
                return None
            except ValueError as e:
                logging.warning(f"API response was not valid JSON: GET {url} -> {e}")
                return None
            finally:
                self.last_request_at = time.time()

    def _request_until_success(self, endpoint: str, is_json: bool = True, base_urls: Optional[List[str]] = None) -> Any:
        urls = base_urls or self.base_urls
        while not STOP_EVENT.is_set():
            for base_url in urls:
                for attempt in range(1, RETRY_ATTEMPTS + 1):
                    if attempt == 1:
                        logging.info(f"Fetching {endpoint} from {base_url}.")
                    else:
                        logging.info(f"Retry {attempt}/{RETRY_ATTEMPTS}: fetching {endpoint} from {base_url}.")
                    result = self._request_once(base_url, endpoint, is_json)
                    if result is not None:
                        if attempt > 1 or base_url != urls[0]:
                            logging.info(f"Recovered API data for {endpoint} from {base_url}.")
                        return result
                    if STOP_EVENT.wait(RETRY_DELAY_SECONDS):
                        raise SystemExit("Shutdown requested while waiting between API retries.")
                logging.warning(f"All {RETRY_ATTEMPTS} attempts failed for {endpoint} on {base_url}. Trying fallback if available.")
            logging.error(f"All APIs failed for {endpoint}. No blockchain data will be skipped. Waiting 5 minutes before retrying.")
            if STOP_EVENT.wait(RETRY_WAIT_AFTER_ALL_FAILURES_SECONDS):
                raise SystemExit("Shutdown requested while waiting for API recovery.")
        raise SystemExit("Shutdown requested.")

    def get_tip_height(self) -> int:
        response = self._request_until_success("blocks/tip/height", is_json=False)
        return int(response.text)

    def get_block_hash(self, height: int) -> str:
        response = self._request_until_success(f"block-height/{height}", is_json=False)
        return response.text.strip()

    def get_raw_block(self, block_hash: str) -> bytes:
        response = self._request_until_success(f"block/{block_hash}/raw", is_json=False)
        return response.content

    def get_transaction(self, txid: str) -> Dict[str, Any]:
        cached = self.tx_cache.get(txid)
        if cached is not None:
            logging.info(f"Using cached transaction details for {txid}; no REST API call needed.")
            return cached
        tx = self._request_until_success(f"tx/{txid}", is_json=True)
        self.tx_cache[txid] = tx
        return tx

    def get_outspends(self, txid: str) -> List[Dict[str, Any]]:
        cached = self.outspends_cache.get(txid)
        if cached is not None:
            logging.info(f"Using cached outspends for TX0 {txid}; no REST API call needed.")
            return cached
        outspends = self._request_until_success(f"tx/{txid}/outspends", is_json=True, base_urls=self.outspends_urls)
        self.outspends_cache[txid] = outspends
        return outspends


class WhirlpoolTracer:
    def __init__(self, fresh_start: bool = False):
        self.db_manager = DatabaseManager(DB_FILE)
        self.client = MempoolClient()
        self.web_started = False
        self.tx0_outspends_in_progress: Set[str] = set()
        self.start_block_height = self._get_earliest_genesis_block_height()
        self.db_manager.setup_db(self.start_block_height, fresh_start)

    def _get_earliest_genesis_block_height(self) -> int:
        logging.info("Determining earliest Whirlpool genesis block height from known pool transactions.")
        min_height = float("inf")
        for pool_name, genesis_info in GENESIS_TXS.items():
            txid = genesis_info["txid"]
            tx_details = self.client.get_transaction(txid)
            if not tx_details.get("status", {}).get("confirmed"):
                raise SystemExit(f"Could not fetch confirmed genesis transaction {txid}")
            height = int(tx_details["status"]["block_height"])
            logging.info(f"Genesis transaction for {POOL_LABELS.get(pool_name, pool_name)} is {txid} in block {height}.")
            min_height = min(min_height, height)
        return int(min_height)

    def _seed_database_with_genesis(self):
        logging.info("Database is not seeded. Adding known Whirlpool genesis transactions and their outputs.")
        genesis_details = []
        for pool_name, genesis_info in GENESIS_TXS.items():
            tx_details = self.client.get_transaction(genesis_info["txid"])
            tx_data = {
                "txid": tx_details["txid"],
                "block_height": tx_details["status"]["block_height"],
                "block_hash": tx_details["status"]["block_hash"],
                "pool_name": pool_name,
            }
            utxos = [
                {"txid": tx_details["txid"], "vout": i, "value_sats": int(vout["value"]), "pool_name": pool_name}
                for i, vout in enumerate(tx_details.get("vout", []))
            ]
            self.db_manager.add_whirlpool_tx_with_utxos(tx_data, utxos)
            genesis_details.append((pool_name, tx_details, tx_data["block_height"]))
            logging.info(f"Seeded {POOL_LABELS.get(pool_name, pool_name)} genesis transaction {tx_details['txid']} with {len(utxos)} outputs.")
        genesis_details.sort(key=lambda item: item[2])
        logging.info("Both configured Whirlpool pool genesis transactions are now present in the database. Recording their direct input TX0 metadata from earliest pool genesis first without chasing outspends yet.")
        for pool_name, tx_details, block_height in genesis_details:
            self._log_whirlpool_inputs_from_json(tx_details, pool_name, block_height, analyze_parent_outspends=False)
        logging.info("All genesis transactions have been seeded. Direct input TX0 metadata is recorded; full TX0 outspend enrichment will happen in chronological block scanning.")

    def _detect_tx0_from_outputs(self, txid: str, outputs: List[Dict[str, Any]], block_height: Optional[int], block_hash: Optional[str],
                                 first_seen_whirlpool_txid: Optional[str] = None, observed_at_block: Optional[int] = None,
                                 spent_vout: Optional[int] = None, spent_input_index: Optional[int] = None) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
        op_return_indices = [i for i, out in enumerate(outputs) if out["is_op_return"] and int(out["value_sats"]) == 0]
        if len(op_return_indices) != 1:
            return None

        matches = []
        for pool_name, pool_info in GENESIS_TXS.items():
            denom = int(pool_info["denomination_sats"])
            coordinator_fee_sats = int(denom * 0.05)
            fee_indices = [i for i, out in enumerate(outputs) if int(out["value_sats"]) == coordinator_fee_sats]
            if len(fee_indices) != 1:
                continue

            excluded = set(op_return_indices + fee_indices)
            premix_candidates = [
                (i, int(out["value_sats"])) for i, out in enumerate(outputs)
                if i not in excluded and denom <= int(out["value_sats"]) <= denom + TX0_PREMIX_EXTRA_SATS_MAX
            ]
            if not (1 <= len(premix_candidates) <= MAX_TX0_PREMIX_OUTPUTS):
                continue
            premix_values = {value for _, value in premix_candidates}
            if len(premix_values) != 1:
                continue
            other_non_special = [i for i, _out in enumerate(outputs) if i not in excluded and i not in {idx for idx, _ in premix_candidates}]
            if len(other_non_special) > 1:
                continue
            if spent_vout is not None and spent_vout not in {idx for idx, _ in premix_candidates}:
                continue

            premix_value = next(iter(premix_values))
            extra_sats = premix_value - denom
            premix_count = len(premix_candidates)
            fee_paid_pct = (coordinator_fee_sats / (premix_count * denom) * 100.0) if premix_count else 0.0
            premix_outputs = []
            for idx, value in premix_candidates:
                premix_outputs.append({
                    "output_id": f"{txid}:{idx}",
                    "tx0_txid": txid,
                    "vout": idx,
                    "value_sats": value,
                    "denomination_sats": denom,
                    "fee_extra_sats": extra_sats,
                    "pool_name": pool_name,
                    "status": TX0_STATUS_UNMIXED,
                    "status_reason": "Detected strict TX0 premix output; no outspend status processed yet.",
                    "spent_txid": None,
                    "spent_vin": None,
                    "spent_block_height": None,
                    "spent_block_hash": None,
                    "spent_tx_is_whirlpool": 0,
                    "spent_tx_pool_name": None,
                    "spent_in_whirlpool_txid": first_seen_whirlpool_txid if idx == spent_vout else None,
                    "spent_input_index": spent_input_index if idx == spent_vout else None,
                })
            tx0 = {
                "txid": txid,
                "pool_name": pool_name,
                "block_height": block_height,
                "block_hash": block_hash,
                "coordinator_fee_sats": coordinator_fee_sats,
                "premix_output_count": premix_count,
                "premix_value_sats": premix_value,
                "premix_fee_extra_sats": extra_sats,
                "fee_paid_pct": fee_paid_pct,
                "first_seen_whirlpool_txid": first_seen_whirlpool_txid,
                "observed_at_block": observed_at_block or block_height,
                "last_status_refresh_block": None,
            }
            matches.append((tx0, premix_outputs))

        if len(matches) != 1:
            return None
        return matches[0]

    def _detect_tx0_from_core_tx(self, tx, block_height: int, block_hash: str) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
        outputs = [
            {"value_sats": int(vout.nValue), "is_op_return": is_zero_sat_op_return(vout)}
            for vout in tx.vout
        ]
        return self._detect_tx0_from_outputs(txid_from_core(tx), outputs, block_height, block_hash)

    def _detect_tx0_from_json(self, tx_details: Dict[str, Any], first_seen_whirlpool_txid: Optional[str] = None,
                              observed_at_block: Optional[int] = None, spent_vout: Optional[int] = None,
                              spent_input_index: Optional[int] = None) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
        status = tx_details.get("status", {})
        outputs = [
            {"value_sats": int(vout.get("value", 0) or 0), "is_op_return": is_json_zero_sat_op_return(vout)}
            for vout in tx_details.get("vout", [])
        ]
        return self._detect_tx0_from_outputs(
            tx_details["txid"],
            outputs,
            status.get("block_height"),
            status.get("block_hash"),
            first_seen_whirlpool_txid=first_seen_whirlpool_txid,
            observed_at_block=observed_at_block,
            spent_vout=spent_vout,
            spent_input_index=spent_input_index,
        )

    def _is_json_whirlpool_tx_for_pool(self, tx_details: Dict[str, Any], pool_name: str) -> bool:
        if len(tx_details.get("vin", [])) != WHIRLPOOL_TX_INPUTS or len(tx_details.get("vout", [])) != WHIRLPOOL_TX_OUTPUTS:
            return False
        denom = GENESIS_TXS[pool_name]["denomination_sats"]
        return all(int(vout.get("value", 0) or 0) == denom for vout in tx_details.get("vout", []))

    def _is_core_whirlpool_tx_for_pool(self, tx, pool_name: str) -> bool:
        if len(tx.vin) != WHIRLPOOL_TX_INPUTS or len(tx.vout) != WHIRLPOOL_TX_OUTPUTS:
            return False
        denom = GENESIS_TXS[pool_name]["denomination_sats"]
        return all(int(vout.nValue) == denom for vout in tx.vout)

    def _record_whirlpool_from_json_if_needed(self, tx_details: Dict[str, Any], pool_name: str, analyze_parent_outspends: bool = True):
        if not self._is_json_whirlpool_tx_for_pool(tx_details, pool_name):
            return
        status = tx_details.get("status", {})
        block_height = status.get("block_height") or 0
        txid = tx_details["txid"]
        tx_data = {
            "txid": txid,
            "block_height": status.get("block_height"),
            "block_hash": status.get("block_hash"),
            "pool_name": pool_name,
        }
        tracked_inputs: Dict[int, sqlite3.Row] = {}
        tracked_pools: Set[str] = set()
        for idx, vin in enumerate(tx_details.get("vin", [])):
            prev_txid = vin.get("txid")
            prev_vout = vin.get("vout")
            if prev_txid is None or prev_vout is None:
                continue
            parent_utxo = self.db_manager.get_unspent_utxo_by_id(f"{prev_txid}:{int(prev_vout)}")
            if parent_utxo:
                tracked_inputs[idx] = parent_utxo
                tracked_pools.add(parent_utxo["pool_name"])

        if tracked_inputs:
            for utxo in tracked_inputs.values():
                self.db_manager.mark_utxo_as_spent(utxo["output_id"], txid, int(block_height))
                logging.info(f"Marked tracked anonymity-set UTXO {utxo['output_id']} as spent by future-discovered Whirlpool cycle {txid} at block {block_height}.")

        if tracked_inputs and len(tracked_pools) == 1 and next(iter(tracked_pools)) == pool_name:
            new_utxos = [
                {"txid": txid, "vout": i, "value_sats": int(vout.get("value", 0) or 0), "pool_name": pool_name}
                for i, vout in enumerate(tx_details.get("vout", []))
            ]
            self.db_manager.add_whirlpool_tx_with_utxos(tx_data, new_utxos)
            logging.info(f"Recorded Whirlpool cycle {txid} with 5 anonymity-set outputs because it spends tracked Whirlpool UTXO(s) from {POOL_LABELS.get(pool_name, pool_name)}.")
        else:
            if tracked_inputs:
                logging.warning(f"TX0-discovered Whirlpool-shaped transaction {txid} NOT recorded as Whirlpool cycle because tracked inputs came from pools {sorted(tracked_pools)} while outputs match {POOL_LABELS.get(pool_name, pool_name)}.")
            else:
                logging.info(f"TX0-discovered Whirlpool-shaped transaction {txid} NOT recorded as Whirlpool cycle because it does not spend currently tracked Whirlpool UTXO(s). This may be a non-Whirlpool CoinJoin with similar structure.")

        self._log_whirlpool_inputs_from_json(tx_details, pool_name, block_height, analyze_parent_outspends=analyze_parent_outspends)
        if not analyze_parent_outspends:
            logging.info(f"Recorded Whirlpool cycle {txid} from TX0 outspend classification and recorded its input TX0 metadata without recursive outspend analysis.")
        logging.info(f"Recorded Whirlpool cycle {txid} for {POOL_LABELS.get(pool_name, pool_name)} from TX0 outspend analysis.")

    def _classify_and_record_input(self, whirlpool_txid: str, input_index: int, prev_txid: str, prev_vout: int,
                                   pool_name: str, block_height: int, remix_utxo: Optional[sqlite3.Row] = None,
                                   analyze_parent_outspends: bool = True):
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
            logging.info(f"Whirlpool input {whirlpool_txid}:{input_index} spends tracked remix UTXO {prev_txid}:{prev_vout}.")
            self.db_manager.record_whirlpool_input(record)
            return

        logging.info(f"Whirlpool input {whirlpool_txid}:{input_index} is not a tracked remix. Fetching parent {prev_txid} to test strict TX0 structure.")
        parent_tx = self.client.get_transaction(prev_txid)
        tx0_info = self._detect_tx0_from_json(parent_tx, first_seen_whirlpool_txid=whirlpool_txid,
                                              observed_at_block=block_height, spent_vout=prev_vout,
                                              spent_input_index=input_index)
        if tx0_info:
            tx0, premix_outputs = tx0_info
            logging.info(f"Strict TX0 detected from Whirlpool input: {prev_txid} ({POOL_LABELS.get(tx0['pool_name'], tx0['pool_name'])}) with {tx0['premix_output_count']} premix outputs.")
            self.db_manager.upsert_tx0(tx0, premix_outputs)
            if analyze_parent_outspends:
                self.analyze_tx0_outspends(prev_txid, trigger=f"Whirlpool input {whirlpool_txid}:{input_index}")
            else:
                logging.info(f"Skipping immediate outspends re-analysis for TX0 {prev_txid}; this Whirlpool input is already being classified during TX0 outspend analysis.")
            record["source_type"] = "tx0"
            record["tx0_txid"] = prev_txid
            record["tx0_vout"] = prev_vout
            try:
                record["value_sats"] = int(parent_tx["vout"][prev_vout]["value"])
            except (IndexError, KeyError, TypeError):
                pass
        else:
            logging.info(f"Parent {prev_txid} did not match strict TX0 structure for {POOL_LABELS.get(pool_name, pool_name)}. Recording it as a fetched non-TX0 external input so metadata is complete, not unknown.")
            record["source_type"] = "external"
            try:
                record["value_sats"] = int(parent_tx["vout"][prev_vout]["value"])
            except (IndexError, KeyError, TypeError):
                pass
        self.db_manager.record_whirlpool_input(record)

    def _log_whirlpool_inputs_from_json(self, tx_details: Dict[str, Any], pool_name: str, block_height: int, analyze_parent_outspends: bool = True):
        for idx, vin in enumerate(tx_details.get("vin", [])):
            prev_txid = vin.get("txid")
            prev_vout = vin.get("vout")
            if prev_txid is None or prev_vout is None:
                continue
            output_id = f"{prev_txid}:{prev_vout}"
            remix_utxo = self.db_manager.get_any_utxo_by_id(output_id)
            self._classify_and_record_input(tx_details["txid"], idx, prev_txid, int(prev_vout), pool_name, block_height, remix_utxo, analyze_parent_outspends=analyze_parent_outspends)

    def analyze_tx0_outspends(self, tx0_txid: str, trigger: str = "scanner"):
        if tx0_txid in self.tx0_outspends_in_progress:
            logging.info(f"TX0 {tx0_txid} outspends analysis is already in progress; skipping duplicate recursive request triggered by {trigger}.")
            return
        row = self.db_manager.query_one("SELECT * FROM tx0s WHERE txid=?", (tx0_txid,))
        if not row:
            return
        outputs = self.db_manager.query_all("SELECT * FROM tx0_premix_outputs WHERE tx0_txid=? ORDER BY vout", (tx0_txid,))
        if not outputs:
            return
        logging.info(f"Analyzing outspends for TX0 {tx0_txid} because {trigger}. Premix outputs: {len(outputs)}.")
        self.tx0_outspends_in_progress.add(tx0_txid)
        try:
            outspends = self.client.get_outspends(tx0_txid)
            outspend_by_vout = {idx: item for idx, item in enumerate(outspends or [])}
            refresh_block = self.db_manager.get_progress("current_processing_block") or row["block_height"] or 0

            for output in outputs:
                vout = int(output["vout"])
                output_id = output["output_id"]
                outspend = outspend_by_vout.get(vout)
                if not outspend or not outspend.get("spent"):
                    if output["status"] != TX0_STATUS_UNMIXED:
                        logging.info(f"TX0 output {output_id} is now unmixed/unspent again according to outspends data.")
                    else:
                        logging.info(f"TX0 output {output_id} remains unmixed and waiting for a Whirlpool cycle.")
                    self.db_manager.update_tx0_output_status(output_id, TX0_STATUS_UNMIXED, "Output is unspent according to outspends data.", None, None, None, None, False, None, None, None)
                    continue

                spend_txid = outspend.get("txid")
                spend_vin = outspend.get("vin")
                spend_status = outspend.get("status", {}) or {}
                spend_height = spend_status.get("block_height")
                spend_hash = spend_status.get("block_hash")
                logging.info(f"TX0 output {output_id} is spent by transaction {spend_txid} input {spend_vin}. Fetching spending transaction from the configured REST API for Whirlpool/non-Whirlpool classification.")
                spend_tx = self.client.get_transaction(spend_txid)
                pool_name = output["pool_name"]
                if self._is_json_whirlpool_tx_for_pool(spend_tx, pool_name):
                    self._record_whirlpool_from_json_if_needed(spend_tx, pool_name, analyze_parent_outspends=False)
                    self.db_manager.update_tx0_output_status(
                        output_id, TX0_STATUS_MIXED, "Output spent into a strict 5-input/5-output Whirlpool cycle.",
                        spend_txid, spend_vin, spend_height, spend_hash, True, pool_name, spend_txid, spend_vin,
                    )
                    logging.info(f"TX0 output {output_id} status: mixed in Whirlpool cycle {spend_txid}.")
                else:
                    self.db_manager.update_tx0_output_status(
                        output_id, TX0_STATUS_EXITED, "Output was spent outside a strict Whirlpool 5x5 cycle.",
                        spend_txid, spend_vin, spend_height, spend_hash, False, None, None, None,
                    )
                    logging.info(f"TX0 output {output_id} status: exited. Spending transaction {spend_txid} is not a strict Whirlpool 5x5 cycle.")
            self.db_manager.mark_tx0_last_refresh(tx0_txid, int(refresh_block))
        finally:
            self.tx0_outspends_in_progress.discard(tx0_txid)

    def refresh_unmixed_tx0_outputs(self):
        logging.info("Refreshing TX0 premix outputs currently marked as unmixed before reports/dashboard refresh.")
        seen_tx0s: Set[str] = set()
        while not STOP_EVENT.is_set():
            rows = self.db_manager.get_unmixed_tx0_outputs(limit=500)
            tx0s = [row["tx0_txid"] for row in rows if row["tx0_txid"] not in seen_tx0s]
            if not tx0s:
                break
            for tx0_txid in tx0s:
                seen_tx0s.add(tx0_txid)
                self.analyze_tx0_outspends(tx0_txid, trigger="interval refresh of unmixed premix outputs")
        self.db_manager.refresh_all_tx0_aggregates()
        logging.info("Completed TX0 unmixed-output refresh.")

    def backfill_missing_input_metadata(self):
        pass_number = 0
        while not STOP_EVENT.is_set():
            missing = self.db_manager.get_whirlpool_txs_missing_inputs()
            if not missing:
                logging.info("No Whirlpool cycles need input/TX0 metadata backfill; cycle metadata is complete.")
                return
            pass_number += 1
            logging.info(f"Backfill pass {pass_number}: completing input/TX0 metadata for {len(missing)} Whirlpool cycle(s) before scanning continues.")
            for index, row in enumerate(missing, start=1):
                logging.info(f"Backfill pass {pass_number}, {index}/{len(missing)}: fetching Whirlpool transaction {row['txid']}.")
                tx_details = self.client.get_transaction(row["txid"])
                self._log_whirlpool_inputs_from_json(tx_details, row["pool_name"], row["block_height"])
            self.db_manager.refresh_all_tx0_aggregates()

    def process_block(self, block: CBlock, block_height: int, block_hash: str):
        logging.info(f"Processing block {block_height} ({block_hash}) with {len(block.vtx)} transactions. First collecting every strict TX0, every strict Whirlpool 5x5 cycle, and every tracked Whirlpool spend in the block.")
        tx0_records: List[Tuple[str, Dict[str, Any], List[Dict[str, Any]]]] = []
        whirlpool_cycle_records: List[Dict[str, Any]] = []
        tracked_spend_records: List[Dict[str, Any]] = []
        seen_whirlpool_cycle_txids: Set[str] = set()

        for tx in block.vtx:
            if tx.is_coinbase():
                continue
            txid = txid_from_core(tx)
            tx0_info = self._detect_tx0_from_core_tx(tx, block_height, block_hash)
            if tx0_info:
                tx0, premix_outputs = tx0_info
                tx0_records.append((txid, tx0, premix_outputs))

            strict_cycle_pool = None
            for candidate_pool in sorted(GENESIS_TXS.keys()):
                if self._is_core_whirlpool_tx_for_pool(tx, candidate_pool):
                    strict_cycle_pool = candidate_pool
                    break

            spent_utxo_pools: Set[str] = set()
            spent_utxos_from_set: Dict[int, sqlite3.Row] = {}
            input_refs: List[Tuple[int, str, int]] = []
            for idx, vin in enumerate(tx.vin):
                prev_txid = prev_txid_from_vin(vin)
                prev_vout = int(vin.prevout.n)
                input_refs.append((idx, prev_txid, prev_vout))
                parent_utxo = self.db_manager.get_unspent_utxo_by_id(f"{prev_txid}:{prev_vout}")
                if parent_utxo:
                    spent_utxo_pools.add(parent_utxo["pool_name"])
                    spent_utxos_from_set[idx] = parent_utxo
            if strict_cycle_pool:
                whirlpool_cycle_records.append({
                    "tx": tx,
                    "txid": txid,
                    "pool_name": strict_cycle_pool,
                    "input_refs": input_refs,
                    "spent_utxos_from_set": spent_utxos_from_set,
                })
                seen_whirlpool_cycle_txids.add(txid)

            if spent_utxos_from_set:
                tracked_spend_records.append({
                    "tx": tx,
                    "txid": txid,
                    "spent_utxo_pools": spent_utxo_pools,
                    "spent_utxos_from_set": spent_utxos_from_set,
                    "input_refs": input_refs,
                })

        logging.info(f"Block {block_height}: collected {len(tx0_records)} strict TX0 candidate(s), {len(whirlpool_cycle_records)} strict Whirlpool 5x5 cycle candidate(s), and {len(tracked_spend_records)} tracked Whirlpool-spend candidate(s). Now enriching all block TX0s sequentially before recording cycles and classifying tracked spends.")
        for txid, tx0, premix_outputs in tx0_records:
            logging.info(
                f"Block {block_height} transaction {txid}: strict TX0 detected for {POOL_LABELS.get(tx0['pool_name'], tx0['pool_name'])}; "
                f"premix outputs={tx0['premix_output_count']}, premix value={tx0['premix_value_sats']} sats, fee paid={tx0['fee_paid_pct']:.2f}%."
            )
            self.db_manager.upsert_tx0(tx0, premix_outputs)
            self.analyze_tx0_outspends(txid, trigger=f"block {block_height} strict TX0 detection")
            self.backfill_missing_input_metadata()

        lineage_tx_count = 0
        for record in whirlpool_cycle_records:
            tx = record["tx"]
            txid = record["txid"]
            pool_name = record["pool_name"]
            input_refs = record["input_refs"]
            spent_utxos_from_set = record["spent_utxos_from_set"]
            tx_data = {"txid": txid, "block_height": block_height, "block_hash": block_hash, "pool_name": pool_name}

            tracked_pools = {utxo["pool_name"] for utxo in spent_utxos_from_set.values()}
            if spent_utxos_from_set:
                for utxo in spent_utxos_from_set.values():
                    self.db_manager.mark_utxo_as_spent(utxo["output_id"], txid, block_height)
                    logging.info(f"Marked tracked anonymity-set UTXO {utxo['output_id']} as spent by {txid} at block {block_height}.")

            if spent_utxos_from_set and len(tracked_pools) == 1 and next(iter(tracked_pools)) == pool_name:
                new_utxos = [{"txid": txid, "vout": i, "value_sats": int(vout.nValue), "pool_name": pool_name} for i, vout in enumerate(tx.vout)]
                self.db_manager.add_whirlpool_tx_with_utxos(tx_data, new_utxos)
                logging.info(f"Block {block_height} transaction {txid}: strict Whirlpool 5x5 cycle recorded for {POOL_LABELS.get(pool_name, pool_name)} with 5 new anonymity-set outputs because it spends tracked Whirlpool UTXO(s).")
                lineage_tx_count += 1
                for idx, prev_txid, prev_vout in input_refs:
                    self._classify_and_record_input(txid, idx, prev_txid, prev_vout, pool_name, block_height, spent_utxos_from_set.get(idx))
                self.backfill_missing_input_metadata()
            else:
                if spent_utxos_from_set:
                    logging.warning(f"Block {block_height} transaction {txid}: strict Whirlpool 5x5 shape detected but NOT recorded as Whirlpool cycle because tracked inputs came from pools {sorted(tracked_pools)} while outputs match {POOL_LABELS.get(pool_name, pool_name)}.")
                else:
                    logging.info(f"Block {block_height} transaction {txid}: strict Whirlpool 5x5 shape detected but NOT recorded as Whirlpool cycle because it does not spend tracked Whirlpool UTXO(s). This may be a non-Whirlpool CoinJoin with similar structure.")

        for record in tracked_spend_records:
            tx = record["tx"]
            txid = record["txid"]
            if txid in seen_whirlpool_cycle_txids:
                continue
            spent_utxo_pools = record["spent_utxo_pools"]
            spent_utxos_from_set = record["spent_utxos_from_set"]
            input_refs = record["input_refs"]
            logging.info(f"Block {block_height} transaction {txid}: spends {len(spent_utxos_from_set)} tracked Whirlpool anonymity-set UTXO(s).")
            for utxo in spent_utxos_from_set.values():
                self.db_manager.mark_utxo_as_spent(utxo["output_id"], txid, block_height)
                logging.info(f"Marked tracked anonymity-set UTXO {utxo['output_id']} as spent by {txid} at block {block_height}.")

            if len(spent_utxo_pools) > 1:
                logging.warning(f"Transaction {txid} mixes tracked UTXOs from multiple pools {sorted(spent_utxo_pools)}. Existing unspent-lineage pruning logic removes this lineage from poolsize.")
                continue

            logging.info(f"Transaction {txid}: spends tracked Whirlpool UTXO(s) but is not a strict 5x5 pool-denomination Whirlpool cycle. Existing unspent-lineage pruning removes it from active poolsize.")

        self.backfill_missing_input_metadata()
        self.db_manager.refresh_all_tx0_aggregates()
        logging.info(f"Finished block {block_height}: fully processed {len(tx0_records)} strict TX0(s) and {lineage_tx_count} Whirlpool cycle(s) before advancing.")

    def run(self, with_web: bool = True):
        logging.info("Starting Whirlpool.Observer scanner.")
        if with_web:
            self.start_web_server()
        if not self.db_manager.is_db_seeded():
            self._seed_database_with_genesis()
        self.backfill_missing_input_metadata()

        scan_started_at = time.time()
        scan_started_height = self.db_manager.get_progress("last_processed_block_height") or self.start_block_height

        try:
            while not STOP_EVENT.is_set():
                last_processed = self.db_manager.get_progress("last_processed_block_height")
                start_block = last_processed + 1 if last_processed is not None else self.start_block_height
                tip_height = self.client.get_tip_height()
                self.db_manager.update_progress("current_tip_height", tip_height)
                logging.info(f"Current chain tip is block {tip_height}. Last processed block is {last_processed if last_processed is not None else 'none'}. Next scan block is {start_block}.")

                if start_block > tip_height:
                    self.db_manager.update_progress("current_processing_block", tip_height)
                    self.refresh_unmixed_tx0_outputs()
                    self.display_stats()
                    self.refresh_reports()
                    self.db_manager.update_progress("last_report_refresh_ts", int(time.time()))
                    logging.info(f"Scanner is caught up to tip block {tip_height}. Waiting {RESCAN_INTERVAL_HOURS:g} hours before checking for new blocks.")
                    if STOP_EVENT.wait(PROCESS_LOOP_DELAY_SECONDS):
                        break
                    scan_started_at = time.time()
                    scan_started_height = self.db_manager.get_progress("last_processed_block_height") or self.start_block_height
                    continue

                for height in range(start_block, tip_height + 1):
                    if STOP_EVENT.is_set():
                        logging.info("Shutdown requested before fetching next block. Progress remains consistent at the last fully processed block.")
                        break
                    self.db_manager.update_progress("current_processing_block", height)
                    logging.info(f"Fetching block {height}/{tip_height}.")
                    block_hash = self.client.get_block_hash(height)
                    raw_block_data = self.client.get_raw_block(block_hash)
                    block = CBlock.deserialize(raw_block_data)
                    self.process_block(block, height, block_hash)
                    self.db_manager.update_progress("last_processed_block_height", height)
                    if height % 100 == 0:
                        elapsed = max(time.time() - scan_started_at, 1)
                        processed = max(height - scan_started_height, 1)
                        remaining = max(tip_height - height, 0)
                        eta = remaining * (elapsed / processed)
                        total_scan_blocks = max(tip_height - self.start_block_height, 1)
                        processed_scan_blocks = max(height - self.start_block_height, 0)
                        progress_pct = min((processed_scan_blocks / total_scan_blocks) * 100, 100)
                        logging.info(f"Progress update: reached block {height}/{tip_height} ({progress_pct:.2f}%). Estimated time remaining: {human_duration(eta)}.")
        except KeyboardInterrupt:
            logging.info("Detector stopped by user keyboard interrupt.")
        finally:
            logging.info("Scanner is shutting down gracefully; refreshing aggregates, displaying stats, and closing SQLite.")
            self.db_manager.refresh_all_tx0_aggregates()
            self.display_stats()
            self.db_manager.close()
            logging.info("Whirlpool.Observer shut down.")

    def display_stats(self):
        logging.info("--- Current Whirlpool Pool Stats ---")
        stats = self.db_manager.get_anonymity_set_stats()
        for pool in sorted(GENESIS_TXS.keys()):
            unspent = stats.get(pool, {"count": 0, "total_sats": 0})
            unmixed = self.db_manager.query_one("SELECT COUNT(*) AS count, COALESCE(SUM(denomination_sats),0) AS sats FROM tx0_premix_outputs WHERE pool_name=? AND status='unmixed'", (pool,))
            poolsize_sats = (unspent["total_sats"] or 0) + (unmixed["sats"] or 0)
            pool_utxos = (unspent["count"] or 0) + (unmixed["count"] or 0)
            logging.info(f"{POOL_LABELS.get(pool, pool)}: poolsize {btc(poolsize_sats):.4f} BTC across {pool_utxos} UTXOs ({unspent['count']} mixed unspent + {unmixed['count'] if unmixed else 0} unmixed premix).")

    def refresh_reports(self):
        logging.info("Refreshing generated CSV reports and PNG charts.")
        os.makedirs(REPORTS_DIR, exist_ok=True)
        prefixes = ("whirlpool_report_", "whirlpool_simplereport_", "whirlpool_capacity_chart_", "whirlpool_utxo_chart_")
        for filename in os.listdir(REPORTS_DIR):
            if filename.startswith(prefixes) and filename.endswith((".csv", ".png")):
                try:
                    os.remove(os.path.join(REPORTS_DIR, filename))
                    logging.info(f"Deleted old generated report artifact {filename}.")
                except OSError as e:
                    logging.error(f"Failed to delete old artifact {filename}: {e}")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.generate_simple_report(interval=10, output_file=os.path.join(REPORTS_DIR, f"whirlpool_simplereport_{timestamp}.csv"))
        self.generate_report(interval=1000, output_file=os.path.join(REPORTS_DIR, f"whirlpool_report_{timestamp}.csv"))
        try:
            self.generate_charts(timestamp=timestamp)
        except Exception as e:
            logging.exception(f"Chart generation failed but scanning/report generation will continue: {e}")

    def _poolsize_events(self) -> List[sqlite3.Row]:
        return self.db_manager.query_all("""
            SELECT COALESCE(t.block_height, t.observed_at_block) AS block, o.pool_name, o.denomination_sats AS value_sats, 1 AS utxo_delta
            FROM tx0_premix_outputs o JOIN tx0s t ON t.txid=o.tx0_txid
            WHERE COALESCE(t.block_height, t.observed_at_block) IS NOT NULL
            UNION ALL
            SELECT o.spent_block_height AS block, o.pool_name, -o.denomination_sats AS value_sats, -1 AS utxo_delta
            FROM tx0_premix_outputs o
            WHERE o.status IN ('mixed','exited','removed') AND o.spent_block_height IS NOT NULL
            UNION ALL
            SELECT t.block_height AS block, u.pool_name, u.value_sats AS value_sats, 1 AS utxo_delta
            FROM anonymity_set_utxos u JOIN whirlpool_txs t ON u.txid=t.txid
            UNION ALL
            SELECT spent_in_block_height AS block, pool_name, -value_sats AS value_sats, -1 AS utxo_delta
            FROM anonymity_set_utxos
            WHERE is_spent=1 AND spent_in_block_height IS NOT NULL
        """)

    def _unspent_capacity_events(self) -> List[sqlite3.Row]:
        return self.db_manager.query_all("""
            SELECT t.block_height AS block, u.pool_name, u.value_sats AS value_sats, 1 AS utxo_delta
            FROM anonymity_set_utxos u JOIN whirlpool_txs t ON u.txid=t.txid
            UNION ALL
            SELECT spent_in_block_height AS block, pool_name, -value_sats AS value_sats, -1 AS utxo_delta
            FROM anonymity_set_utxos
            WHERE is_spent=1 AND spent_in_block_height IS NOT NULL
        """)

    def _build_stepped_pool_series(self, rows: List[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        events = [row for row in rows if row["block"] is not None]
        if not events:
            return None
        events.sort(key=lambda row: row["block"])
        pool_names = sorted(GENESIS_TXS.keys())
        cumulative = {pool: 0 for pool in pool_names}
        blocks = []
        series = {pool: [] for pool in pool_names}
        idx = 0
        while idx < len(events):
            block = events[idx]["block"]
            while idx < len(events) and events[idx]["block"] == block:
                pool = events[idx]["pool_name"]
                if pool in cumulative:
                    cumulative[pool] += events[idx]["value_sats"] or 0
                    cumulative[pool] = max(cumulative[pool], 0)
                idx += 1
            blocks.append(block)
            for pool in pool_names:
                series[pool].append(btc(cumulative[pool]))
        return {"blocks": blocks, "series": series}

    def _build_pool_capacity_chart_data(self) -> Optional[Dict[str, Any]]:
        return self._build_stepped_pool_series(self._poolsize_events())

    def _build_unspent_capacity_chart_data(self) -> Optional[Dict[str, Any]]:
        return self._build_stepped_pool_series(self._unspent_capacity_events())

    def _build_total_utxo_chart_data_from_rows(self, rows: List[sqlite3.Row]) -> Optional[Dict[str, List[int]]]:
        events = [row for row in rows if row["block"] is not None]
        if not events:
            return None
        events.sort(key=lambda row: row["block"])
        blocks, total_utxos = [], []
        cumulative = 0
        idx = 0
        while idx < len(events):
            block = events[idx]["block"]
            while idx < len(events) and events[idx]["block"] == block:
                cumulative += events[idx]["utxo_delta"] or 0
                cumulative = max(cumulative, 0)
                idx += 1
            blocks.append(block)
            total_utxos.append(cumulative)
        return {"blocks": blocks, "total_utxos": total_utxos}

    def _build_total_utxo_chart_data(self) -> Optional[Dict[str, List[int]]]:
        return self._build_total_utxo_chart_data_from_rows(self._poolsize_events())

    def _build_total_unspent_utxo_chart_data(self) -> Optional[Dict[str, List[int]]]:
        return self._build_total_utxo_chart_data_from_rows(self._unspent_capacity_events())

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
        self._style_chart(ax, "Whirlpool Poolsize by Pool", "Poolsize (BTC)", MaxNLocator)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.2f}"))
        ax.legend(loc="upper right", frameon=True, facecolor="white", edgecolor="#d8dde6")
        fig.tight_layout()
        fig.savefig(filename, bbox_inches="tight")
        plt.close(fig)

    def _write_utxo_chart(self, plt, FuncFormatter, MaxNLocator, chart_data: Dict[str, List[int]], filename: str):
        fig, ax = plt.subplots(figsize=(14, 7), dpi=160)
        fig.patch.set_facecolor("white")
        ax.plot(chart_data["blocks"], chart_data["total_utxos"], drawstyle="steps-post", linewidth=2.5, color="#16a34a", label="UTXOs in Whirlpool")
        ax.fill_between(chart_data["blocks"], chart_data["total_utxos"], step="post", alpha=0.10, color="#16a34a")
        self._style_chart(ax, "Total UTXOs in Whirlpool", "UTXOs", MaxNLocator)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{int(value):,}"))
        ax.legend(loc="upper right", frameon=True, facecolor="white", edgecolor="#d8dde6")
        fig.tight_layout()
        fig.savefig(filename, bbox_inches="tight")
        plt.close(fig)

    def generate_report(self, interval: int = 1000, output_file: Optional[str] = None):
        rows = self._capacity_rows(interval)
        if output_file and rows:
            header = ["end_block", "total_poolsize_btc"] + [f"poolsize_{name}_btc" for name in sorted(GENESIS_TXS.keys())]
            self._write_chart_report_to_csv(output_file, header, rows)

    def _capacity_rows(self, interval: int) -> List[Dict[str, Any]]:
        summary = self.build_summary()
        current_block = summary["last_processed_block"] or summary["tip_height"] or 0
        row = {"end_block": ((current_block // interval) + 1) * interval, "total_poolsize_btc": sum(p["poolsize_btc"] for p in summary["pools"])}
        for pool in summary["pools"]:
            row[f"poolsize_{pool['pool']}_btc"] = pool["poolsize_btc"]
        return [row]

    def _write_chart_report_to_csv(self, filename: str, header: List[str], rows: List[Dict[str, Any]]):
        os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)
        with open(filename, "w", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=header)
            writer.writeheader()
            writer.writerows(rows)
        logging.info(f"Report saved to {filename}")

    def generate_simple_report(self, interval: int = 10, output_file: Optional[str] = None):
        summary = self.build_summary()
        row = {
            "end_block": summary["last_processed_block"],
            "total_poolsize_btc": sum(p["poolsize_btc"] for p in summary["pools"]),
            "total_utxos_in_pool": sum(p["utxos_in_pool"] for p in summary["pools"]),
        }
        if output_file:
            self._write_chart_report_to_csv(output_file, list(row.keys()), [row])

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

        @app.route("/")
        def index():
            template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "observer.html")
            with open(template_path, "r", encoding="utf-8") as template_file:
                return template_file.read()

        @app.route("/explainer.md")
        def explainer_markdown():
            template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "explainer.md")
            with open(template_path, "r", encoding="utf-8") as template_file:
                return template_file.read(), 200, {"Content-Type": "text/markdown; charset=utf-8"}

        @app.route("/manifest.webmanifest")
        def manifest():
            return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "manifest.webmanifest", mimetype="application/manifest+json")

        @app.route("/sw.js")
        def service_worker():
            return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "sw.js", mimetype="application/javascript")

        @app.route("/assets/<path:filename>")
        def assets(filename):
            return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets"), filename)

        @app.route("/favicon.ico")
        def favicon():
            return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets"), "Ashigaru_Whirlpool_Logo_White.png", mimetype="image/png")

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
            per_page = min(max(int(request.args.get("per_page", 10)), 1), 10)
            return jsonify(tracer.list_whirlpool_txs(pool=pool, page=page, per_page=per_page))

        @app.route("/api/tx0s")
        def api_tx0s():
            page = max(int(request.args.get("page", 1)), 1)
            per_page = min(max(int(request.args.get("per_page", 10)), 1), 10)
            unmixed_only = request.args.get("unmixed_only", "0") == "1"
            exited_only = request.args.get("exited_only", "0") == "1"
            return jsonify(tracer.list_tx0s(page=page, per_page=per_page, unmixed_only=unmixed_only, exited_only=exited_only))

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
            unmixed = self.db_manager.query_one("SELECT COUNT(*) AS count, COALESCE(SUM(denomination_sats),0) AS sats FROM tx0_premix_outputs WHERE pool_name=? AND status='unmixed'", (pool,))
            tx0_stats = self.db_manager.query_one("SELECT COUNT(*) AS tx0s, COALESCE(SUM(premix_output_count),0) AS total_premix, COALESCE(SUM(unmixed_count),0) AS unmixed_count, COALESCE(SUM(mixed_count),0) AS mixed_count, COALESCE(SUM(exited_count),0) AS exited_count, COALESCE(SUM(removed_count),0) AS removed_count, AVG(fee_paid_pct) AS avg_fee_paid FROM tx0s WHERE pool_name=?", (pool,))
            cycles = self.db_manager.query_one("SELECT COUNT(*) AS count FROM whirlpool_txs WHERE pool_name=?", (pool,))
            poolsize_sats = (unspent["sats"] or 0) + (unmixed["sats"] or 0)
            utxos_in_pool = (unspent["count"] or 0) + (unmixed["count"] or 0)
            pools.append({
                "pool": pool,
                "label": POOL_LABELS.get(pool, pool),
                "color": POOL_COLORS.get(pool, "#888"),
                "poolsize_btc": btc(poolsize_sats),
                "utxos_in_pool": utxos_in_pool,
                "unspent_btc": btc(unspent["sats"] or 0),
                "unspent_utxos": unspent["count"] or 0,
                "unmixed_btc": btc(unmixed["sats"] or 0),
                "unmixed_utxos": unmixed["count"] or 0,
                "tx0_count": tx0_stats["tx0s"] or 0,
                "total_premix_outputs": tx0_stats["total_premix"] or 0,
                "mixed_count": tx0_stats["mixed_count"] or 0,
                "exited_count": tx0_stats["exited_count"] or 0,
                "removed_count": tx0_stats["removed_count"] or 0,
                "avg_fee_paid_pct": tx0_stats["avg_fee_paid"] or 0,
                "cycles": cycles["count"] or 0,
            })
        return {
            "title": "Whirlpool.Observer",
            "api_url": MEMPOOL_API_BASE_URL,
            "fallback_api_url": FALLBACK_API_BASE_URL,
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
        poolsize = self._build_pool_capacity_chart_data() or {"blocks": [], "series": {}}
        capacity = self._build_unspent_capacity_chart_data() or {"blocks": [], "series": {}}
        utxos_in_pool = self._build_total_utxo_chart_data() or {"blocks": [], "total_utxos": []}
        utxos = self._build_total_unspent_utxo_chart_data() or {"blocks": [], "total_utxos": []}
        return {
            "poolsize": poolsize,
            "capacity": capacity,
            "utxos_in_pool": utxos_in_pool,
            "utxos": utxos,
        }

    def list_whirlpool_txs(self, pool: Optional[str] = None, page: int = 1, per_page: int = 10) -> Dict[str, Any]:
        page = max(page, 1)
        per_page = min(max(per_page, 1), 10)
        offset = (page - 1) * per_page
        where = "WHERE w.pool_name = ?" if pool in GENESIS_TXS else ""
        params: Tuple[Any, ...] = (pool,) if where else ()
        total_row = self.db_manager.query_one(f"SELECT COUNT(*) AS count FROM whirlpool_txs w {where}", params)
        rows = self.db_manager.query_all(f"""
            SELECT w.txid, w.block_height, w.pool_name,
                   GROUP_CONCAT(CASE WHEN i.source_type='tx0' THEN i.tx0_txid || '|' || COALESCE(printf('%.2f', t.fee_paid_pct), 'n/a') ELSE NULL END, ';;') AS tx0_inputs
            FROM whirlpool_txs w
            LEFT JOIN tx_inputs i ON i.whirlpool_txid=w.txid
            LEFT JOIN tx0s t ON t.txid=i.tx0_txid
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
                    tx0_inputs.append({"txid": txid, "fee_paid_pct": efficiency})
            items.append({
                "txid": row["txid"],
                "block_height": row["block_height"],
                "pool_name": row["pool_name"],
                "pool_label": POOL_LABELS.get(row["pool_name"], row["pool_name"]),
                "pool_color": POOL_COLORS.get(row["pool_name"], "#8e8e93"),
                "tx0_inputs": tx0_inputs,
                "am_i_exposed_url": f"http://am-i.exposed/#tx={row['txid']}",
            })
        total = total_row["count"] if total_row else 0
        return {"items": items, "page": page, "per_page": per_page, "total": total, "total_pages": max((total + per_page - 1) // per_page, 1)}

    def list_tx0s(self, page: int = 1, per_page: int = 10, unmixed_only: bool = False, exited_only: bool = False) -> Dict[str, Any]:
        page = max(page, 1)
        per_page = min(max(per_page, 1), 10)
        offset = (page - 1) * per_page
        clauses = []
        if unmixed_only:
            clauses.append("unmixed_count > 0")
        if exited_only:
            clauses.append("exited_count > 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total_row = self.db_manager.query_one(f"SELECT COUNT(*) AS count FROM tx0s {where}")
        rows = self.db_manager.query_all(f"""
            SELECT * FROM tx0s {where}
            ORDER BY COALESCE(block_height, observed_at_block) DESC, txid DESC
            LIMIT ? OFFSET ?
        """, (per_page, offset))
        items = []
        for row in rows:
            items.append({
                "txid": row["txid"],
                "block_height": row["block_height"] or row["observed_at_block"],
                "pool_name": row["pool_name"],
                "pool_label": POOL_LABELS.get(row["pool_name"], row["pool_name"]),
                "pool_color": POOL_COLORS.get(row["pool_name"], "#8e8e93"),
                "premix_output_count": row["premix_output_count"] or 0,
                "unmixed_count": row["unmixed_count"] or 0,
                "mixed_count": row["mixed_count"] or 0,
                "exited_count": row["exited_count"] or 0,
                "removed_count": row["removed_count"] or 0,
                "poolsize_btc": btc(row["poolsize_sats"] or 0),
                "fee_paid_pct": row["fee_paid_pct"] or 0,
                "am_i_exposed_url": f"http://am-i.exposed/#tx={row['txid']}",
            })
        total = total_row["count"] if total_row else 0
        return {"items": items, "page": page, "per_page": per_page, "total": total, "total_pages": max((total + per_page - 1) // per_page, 1), "unmixed_only": unmixed_only, "exited_only": exited_only}

    def tx_detail(self, txid: str) -> Dict[str, Any]:
        tx = self.db_manager.query_one("SELECT * FROM whirlpool_txs WHERE txid=?", (txid,))
        inputs = self.db_manager.query_all("SELECT * FROM tx_inputs WHERE whirlpool_txid=? ORDER BY input_index", (txid,))
        tx0 = self.db_manager.query_one("SELECT * FROM tx0s WHERE txid=?", (txid,))
        tx0_outputs = self.db_manager.query_all("SELECT * FROM tx0_premix_outputs WHERE tx0_txid=? ORDER BY vout", (txid,))
        return {
            "tx": dict(tx) if tx else None,
            "inputs": [dict(i) for i in inputs],
            "tx0": dict(tx0) if tx0 else None,
            "tx0_outputs": [dict(o) for o in tx0_outputs],
            "am_i_exposed_url": f"http://am-i.exposed/#tx={txid}",
        }


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
