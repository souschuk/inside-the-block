#!/usr/bin/env python3
"""Where our probes stood among everyone else's transactions in the same blocks. Read-only, costs nothing.

For each landed probe it reads the whole block and asks three things: how far from the top it landed, how it ranks by Agave
priority among the block's transactions, and how many transactions with lower priority landed ahead of it, or with higher
priority behind it. Priority comes from the wire bytes, so v1 counts its header limits and a transaction with no budget
instruction counts its defaults. Our own probes must come out at the priority in the journal, or the tool stops.
Usage: TIEPAIRS_RPC=... competition.py RUN_DIR [RUN_DIR ...]
"""
import base64
import collections
import json
import os
import statistics
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from tiepairs import results, tx  # noqa: E402
from tiepairs.rpc import PUBLIC_RPC, Rpc  # noqa: E402

BUILTINS = {tx.SYSTEM_PROGRAM, tx.COMPUTE_BUDGET, 'Vote111111111111111111111111111111111111111', 'BPFLoaderUpgradeab1e11111111111111111111111',
            'Ed25519SigVerify111111111111111111111111111', 'KeccakSecp256k11111111111111111111111111111', 'Secp256r1SigVerify1111111111111111111111111'}
DEFAULT_LOADED = 64 * 1024 * 1024
MAX_LIMIT = 1_400_000


def parse_v1(raw):
    signers, readonly_signed, readonly_unsigned = raw[1:4]
    mask = struct.unpack('<I', raw[4:8])[0]
    n_ix, n_keys = raw[40], raw[41]
    at = 42
    keys = [raw[at + 32 * i:at + 32 * (i + 1)] for i in range(n_keys)]
    at += 32 * n_keys
    config = {}
    for bit, name, size in ((0, 'priority_fee', 8), (2, 'cu_limit', 4), (3, 'loaded', 4), (4, 'heap', 4)):
        if mask >> bit & 1:
            config[name] = int.from_bytes(raw[at:at + size], 'little')
            at += size
    headers = [struct.unpack('<BBH', raw[at + 4 * i:at + 4 * (i + 1)]) for i in range(n_ix)]
    at += 4 * n_ix
    instructions = []
    for program, n_accounts, n_data in headers:
        instructions.append((program, raw[at + n_accounts:at + n_accounts + n_data]))
        at += n_accounts + n_data
    return dict(version=1, signature=raw[at:at + 64], signers=signers, readonly_signed=readonly_signed, readonly_unsigned=readonly_unsigned,
                keys=keys, instructions=instructions, config=config)


def parse_legacy(raw):
    """Legacy and v0: the same layout, v0 has one version byte in front and lookups behind."""
    count, at = tx.read_compact_u16(raw, 0)
    signature = raw[at:at + 64]
    at += 64 * count
    version = None
    if raw[at] & 0x80:
        version = raw[at] & 0x7f
        at += 1
    signers, readonly_signed, readonly_unsigned = raw[at:at + 3]
    at += 3
    n_keys, at = tx.read_compact_u16(raw, at)
    keys = [raw[at + 32 * i:at + 32 * (i + 1)] for i in range(n_keys)]
    at += 32 * n_keys + 32
    n_ix, at = tx.read_compact_u16(raw, at)
    instructions = []
    for _ in range(n_ix):
        program = raw[at]
        at += 1
        length, at = tx.read_compact_u16(raw, at)
        at += length
        length, at = tx.read_compact_u16(raw, at)
        instructions.append((program, raw[at:at + length]))
        at += length
    return dict(version=version, signature=signature, signers=signers, readonly_signed=readonly_signed, readonly_unsigned=readonly_unsigned,
                keys=keys, instructions=instructions, config=None)


def requested(parsed):
    """CU limit, priority fee and loaded data limit a transaction asked for."""
    if parsed['config'] is not None:
        config = parsed['config']
        return config.get('cu_limit', 0), config.get('priority_fee', 0), config.get('loaded', 0)
    limit = price = loaded = None
    default = 0
    for program, data in parsed['instructions']:
        name = tx.b58encode(parsed['keys'][program]) if program < len(parsed['keys']) else ''
        default += 3_000 if name in BUILTINS else 200_000
        if name == tx.COMPUTE_BUDGET and data:
            if data[0] == 2 and len(data) >= 5:
                limit = struct.unpack('<I', data[1:5])[0]
            elif data[0] == 3 and len(data) >= 9:
                price = struct.unpack('<Q', data[1:9])[0]
            elif data[0] == 4 and len(data) >= 5:
                loaded = struct.unpack('<I', data[1:5])[0]
    limit = min(default if limit is None else limit, MAX_LIMIT)
    return limit, (limit * (price or 0) + 999_999) // 1_000_000, DEFAULT_LOADED if loaded is None else loaded


def priority(parsed, meta):
    """Reward per requested cost, the scheduler's key. Reward is the priority fee and half of everything else the fee holds, which
    also covers precompile signatures. The cost leaves out what a precompile signature adds."""
    limit, priority_fee, loaded = requested(parsed)
    base = max((meta.get('fee') or 0) - priority_fee, 0)
    n_keys = len(parsed['keys'])
    writable = n_keys - parsed['readonly_signed'] - parsed['readonly_unsigned'] + len((meta.get('loadedAddresses') or {}).get('writable') or [])
    data_bytes = sum(len(data) for _, data in parsed['instructions'])
    pages = (loaded + tx.LOADED_PAGE_BYTES - 1) // tx.LOADED_PAGE_BYTES
    cost = limit + tx.SIGNATURE_COST * parsed['signers'] + tx.WRITE_LOCK_UNITS * writable + data_bytes // tx.DATA_BYTES_PER_CU + pages * tx.LOADED_PAGE_COST
    return (priority_fee + base - base // 2) * tx.PRIORITY_MULTIPLIER // (cost + 1)


def block_rows(rpc, slot):
    block = rpc.call('getBlock', [slot, dict(encoding='base64', transactionDetails='full', rewards=False, maxSupportedTransactionVersion=1, commitment='confirmed')])
    rows = []
    for t in block.get('transactions') or []:
        raw = base64.b64decode(t['transaction'][0])
        parsed = parse_v1(raw) if raw[0] == tx.V1_VERSION else parse_legacy(raw)
        rows.append((tx.b58encode(parsed['signature']), priority(parsed, t['meta'])))
    return rows


def probe_row(rows, signature):
    position = next(i for i, (s, _) in enumerate(rows) if s == signature)
    own = rows[position][1]
    return dict(position=position, size=len(rows), rank=sum(1 for _, p in rows if p > own), ahead_lower=sum(1 for _, p in rows[:position] if p < own),
                behind_higher=sum(1 for _, p in rows[position + 1:] if p > own), priority=own)


def collect(rpc, run_dir, out):
    cache = {}
    with open(os.path.join(run_dir, 'outcomes.jsonl')) as f:
        outcomes = [json.loads(line) for line in f]
    planned = {p[f'sig_{role}']: p.get(f'priority_{role}', p.get('priority')) for p in results.load_pairs(run_dir) for role in ('a', 'b') if p.get(f'sig_{role}')}
    for o in outcomes:
        for member, role in ((o.get('first'), o['first_sent']), (o.get('second'), 'a' if o['first_sent'] == 'b' else 'b')):
            if not member or member.get('err'):
                continue
            if member['slot'] not in cache:
                cache[member['slot']] = block_rows(rpc, member['slot'])
            rows = cache[member['slot']]
            row = probe_row(rows, o[f'sig_{role}'])
            if row['priority'] != planned[o[f'sig_{role}']]:
                raise SystemExit(f'{o[f"sig_{role}"]}: this arithmetic gives {row["priority"]}, the journal {planned[o[f"sig_{role}"]]}')
            out.append(dict(row, run=o.get('run'), seq=o['seq'], identity=o['identity'], group=o['group'], client=o.get('client'),
                            version=o.get('version'), contrast=o.get('contrast') or '', regime=o.get('regime') or 'buffered', slot_in_window=member['slot'] - o['first_slot']))


def summary(rows):
    by = collections.defaultdict(list)
    for r in rows:
        by[(r['client'], r['version'], r['regime'])].append(r)
    lines = ['| Client | Regime | Probes | Median position | Median priority rank | Block size | Cheaper ahead | Dearer behind |', '|---|---|---:|---:|---:|---:|---:|---:|']
    for (client, version, regime), group in sorted(by.items(), key=str):
        median = lambda key: statistics.median(r[key] for r in group)  # noqa: E731
        lines.append(f'| {client} {version} | {regime} | {len(group)} | {median("position"):.0f} | {median("rank"):.0f} | {median("size"):.0f} | {median("ahead_lower"):.0f} | {median("behind_higher"):.0f} |')
    return '\n'.join(lines) + '\n'


def main():
    rpc = Rpc(os.environ.get('TIEPAIRS_RPC', PUBLIC_RPC), pause=0.1)
    rows = []
    for run_dir in sys.argv[1:]:
        collect(rpc, run_dir, rows)
    for run_dir in sys.argv[1:]:
        with open(os.path.join(run_dir, 'competition.jsonl'), 'w') as f:
            for row in rows:
                if row['run'] == os.path.basename(run_dir.rstrip('/')):
                    f.write(json.dumps(row) + '\n')
    print(summary(rows))


if __name__ == '__main__':
    main()
