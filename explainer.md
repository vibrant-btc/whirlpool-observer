# Whirlpool.Observer Explainer

Whirlpool.Observer is an open-source dashboard and scanner for Whirlpool activity on Bitcoin's open distributed ledger, also called the blockchain.

It only reads public Bitcoin transaction data and applies strict rules to identify transactions that match Whirlpool structures.

This tool utilizes a novel TX0 Transaction scanning and categorization system to ensure only an accurate view of current mixable candidates is presented.

Entered capacity is misleading as it does not account for UTXOs that have "exited" their whirlpool denomination and are no longer elegible for mixing.

As a result this tool's Unspent postmix data remains in line with that of Clark Moody's Dashboard, however this takes that one step further by tracking all TX0 transaction and their output's future outspends to track unmixed premix UTXOs.

Read further to understand more detail behind the logic of this tool.

---

## Whirlpool in Short

Whirlpool is a Bitcoin CoinJoin protocol. In a CoinJoin, multiple people combine inputs into one transaction so it is harder for outside observers to know which input paid which output.

A normal Whirlpool cycle has:

- 5 inputs,
- 5 outputs,
- all outputs at the same exact value.

For example, the 0.025 BTC pool creates five 0.025 BTC outputs. Because the outputs are equal, the transaction does not directly reveal which participant received which output.

Whirlpool.Observer tracks the two current Ashigaru Whirlpool pools:

| Pool | Denomination | Entry fee |
|---|---:|---:|
| 0.025 BTC Pool | 0.025 BTC | 0.00125 BTC |
| 0.25 BTC Pool | 0.25 BTC | 0.0125 BTC |

---

## Key Terms

### UTXO

A UTXO is an unspent transaction output. Bitcoin does not work like one account balance. It works more like separate coins or notes. Each UTXO is one spendable piece of bitcoin.

### TX0

A TX0 is the preparation transaction before a user enters Whirlpool. It splits a user's bitcoin into one or more premix outputs and pays the Whirlpool coordinator fee.

A strict TX0 transaction has:

- one zero-sat OP_RETURN output,
- one coordinator-fee output worth 5% of the pool denomination,
- 1 to 20 premix outputs,
- all premix outputs at the same value,
- premix outputs at the pool denomination, with a small extra amount for miner fees.

Whirlpool.Observer uses this structure to detect TX0s from public blockchain data.

### Premix output

A premix output is created by a TX0. In this tool, a premix output can have the following statuses:

| Status | Meaning |
|---|---|
| Unmixed | The premix output is still unspent and waiting to mix. |
| Mixed | The premix output was spent into a Whirlpool cycle. |
| Exited | The premix output was spent outside a strict Whirlpool cycle. |

### Postmix output

A postmix output is created by a Whirlpool cycle. If it remains unspent, it is counted as part of the tracked Whirlpool postmix set.

### Whirlpool cycle

A Whirlpool cycle is a CoinJoin transaction matching the pool shape:

- 5 inputs,
- 5 outputs,
- all outputs equal to one tracked pool denomination.

Structure alone is not enough for Whirlpool.Observer to count it as active postmix liquidity. The transaction must also spend a tracked Whirlpool UTXO from the correct pool's lineage.

---

## The Whirlpool Process

1. **Deposit:** A user starts with normal bitcoin that has not yet gone through Whirlpool.
2. **TX0:** The wallet creates premix outputs and pays the coordinator fee.
3. **Premix queue:** Premix outputs wait to enter a Whirlpool cycle.
4. **First mix:** A premix output enters a 5-input, 5-output Whirlpool CoinJoin and becomes postmix.
5. **Remix:** Postmix outputs can enter later Whirlpool cycles again.

Remixing can increase the tracked crowd. If one old tracked postmix output remixes, it is spent and five new equal outputs are created:

```text
5 new outputs - 1 spent output = +4 net outputs
```

This is the basic idea behind forward-looking anonymity sets.

---

## What Whirlpool.Observer Tracks

Whirlpool.Observer tracks two buckets:

1. **Waiting to mix:** unmixed TX0 premix outputs.
2. **Already mixed and still unspent:** tracked Whirlpool postmix outputs.

The dashboard's poolsize metric combines both:

```text
poolsize = unmixed premix + unspent tracked postmix
```

The stricter postmix-only metric is:

```text
unspent Whirlpool postmix = unspent tracked postmix
```

---

## Lineage: Why Tracking History Matters

Lineage means following coins forward from known starting points.

Whirlpool.Observer starts from known Whirlpool genesis transactions. Their outputs are inserted into the tracked postmix set. Then each block is scanned in order.

For any transaction that spends a tracked output:

1. The old tracked output is marked spent.
2. The scanner checks whether the spending transaction is a valid Whirlpool cycle for the same pool.
3. If it is valid, the new equal outputs are added to the tracked set.
4. If it is not valid, that branch stops.

This prevents unrelated transactions with a similar 5-input/5-output shape from inflating the active Whirlpool set.

The important rule is:

> A transaction can extend the tracked postmix set only if it spends tracked Whirlpool UTXOs from the correct pool's lineage.

---

## TX0 Tracking

TX0s are different from postmix lineage. A TX0 can be detected directly from the block scan, even before its premix outputs enter Whirlpool.

TX0's outputs represent coins waiting to enter the Whirlpool system, so unspent premix outputs count toward total poolsize, but they are not postmix yet.

When a strict TX0 is detected, Whirlpool.Observer records:

- the TX0 transaction ID,
- block height,
- pool,
- coordinator fee,
- premix output count,
- premix output value,
- effective fee percentage,
- each premix output's status.

The scanner then checks outspends to see whether each premix output is still waiting, entered a Whirlpool cycle, or exited somewhere else.

---

## Dashboard Metrics

### Total BTC in Whirlpool

The total BTC counted across both buckets:

```text
Total BTC in Whirlpool = unmixed premix BTC + unspent Whirlpool postmix BTC
```

This can be slightly larger than a postmix-only tracker because it includes premix outputs waiting to mix.

### Total UTXOs in Whirlpool

The UTXO count version of total poolsize:

```text
Total UTXOs in Whirlpool = unmixed premix UTXOs + unspent Whirlpool postmix UTXOs
```

### Unspent Whirlpool Postmix

This shows only tracked postmix outputs that have completed a Whirlpool CoinJoin and remain unspent in their pool's denomination. It does not include unmixed premix outputs.

### Total Unspent UTXOs

This is the count version of unspent Whirlpool postmix. It counts tracked postmix UTXOs that remain unspent.

### TX0 count

The number of strict TX0 transactions detected for a pool.

### Premix UTXOs Created

The number of premix outputs created by a TX0.

### Unmixed

Premix outputs that are still unspent and waiting to enter Whirlpool.

### Mixed

Premix outputs that were spent into strict Whirlpool cycles.

### Exited

Premix outputs that were spent outside strict Whirlpool cycles.

### Fee paid

The coordinator fee divided by the total value of premix UTXOs created. A TX0 with more premix outputs spreads the fixed fee across more outputs, so the effective percentage is lower.

---

## How the Scanner Works

Whirlpool.Observer scans full raw Bitcoin blocks in order. For each block it checks:

1. Does any transaction match strict TX0 structure?
2. Does any transaction match strict Whirlpool cycle structure?
3. Does any transaction spend a currently tracked Whirlpool UTXO?

API requests are made sequentially with a delay to avoid overloading public endpoints. If required blockchain data cannot be fetched, the scanner **waits and retries instead of silently skipping data**.

Duplicate recording is avoided with unique database keys:

- Whirlpool transaction rows are keyed by transaction ID.
- Tracked outputs are keyed as `txid:vout`.
- TX0 premix outputs are also keyed as `txid:vout`.

---

## Why Total Poolsize Can Be Larger Than Postmix-Only Capacity

The postmix-only metric tracks coins that already completed Whirlpool and remain in the tracked set.

Total poolsize adds unmixed premix outputs that are still waiting to enter Whirlpool:

```text
Total poolsize = unspent Whirlpool postmix + unmixed premix
```

---

## Summary

Whirlpool.Observer reads public Bitcoin blockchain data to track Whirlpool activity. It records strict TX0s, follows tracked Whirlpool postmix lineage, and classifies premix outputs as unmixed, mixed, or exited.

The central rule is simple:

> TX0 premix outputs can count while they are waiting to mix, but Whirlpool cycle outputs only extend the postmix set if they spend tracked Whirlpool UTXOs from the correct pool's lineage.

This keeps the dashboard useful without overstating active Whirlpool postmix liquidity.
