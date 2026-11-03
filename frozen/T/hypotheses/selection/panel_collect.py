# Passive panel: for each validator and epoch, sample leader windows and store a compact priority profile of the blocks.
# Read-only: sends no transactions. Resumes from the output file, so it can be stopped and started at will.
# Usage: TIEPAIRS_RPC=... panel_collect.py targets.json out.jsonl [first_epoch last_epoch windows_per_epoch]
import json, math, os, random, sys, time, urllib.error, urllib.request

CB = 'ComputeBudget111111111111111111111111111111'
B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
IDX = {c: i for i, c in enumerate(B58)}
EPOCH_SLOTS = 432000
EPOCH_1073_START = 463536000
ALPENGLOW_SLOT = 459653008
OFFSETS = (0, 2)  # first and third slot of the 4-slot window


def b58d(s):
    n = 0
    for c in s:
        n = n * 58 + IDX[c]
    b = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return b'\0' * (len(s) - len(s.lstrip('1'))) + b


def rpc(url, method, params, tries=8):
    body = json.dumps(dict(jsonrpc='2.0', id=1, method=method, params=params)).encode()
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, body, {'content-type': 'application/json'})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f'{method}: HTTP {e.code}') from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
            pass
        time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f'{method}: gave up after {tries} tries')


def priority(t):
    """Same arithmetic as pilot.py, so the numbers stay comparable with the published profile."""
    m = t['meta']; tx = t['transaction']; msg = tx['message']; keys = msg['accountKeys']
    sigs = len(tx['signatures']); h = msg['header']; pf = max(m.get('fee', 0) - 5000 * sigs, 0)
    reward = pf + 2500 * sigs
    if str(t.get('version')) == '1':
        cost = m.get('costUnits') or 2000
    else:
        lim = None; non = 0
        for ins in msg['instructions']:
            pid = ins['programIdIndex']; p = keys[pid] if pid < len(keys) else ''
            if p == CB:
                d = b58d(ins['data'])
                if d and d[0] == 2 and len(d) >= 5:
                    lim = int.from_bytes(d[1:5], 'little')
            else:
                non += 1
        if lim is None:
            lim = min(200000 * max(non, 1), 1400000)
        w = h['numRequiredSignatures'] - h['numReadonlySignedAccounts'] + len(keys) - h['numRequiredSignatures'] - h['numReadonlyUnsignedAccounts']
        cost = lim + 720 * sigs + 300 * max(w, 1)
    return reward * 1e6 / (cost + 1), pf


def plan(url, targets, first, last, per_epoch, tip):
    rng = random.Random(20261031)
    todo = []
    for epoch in range(first, last + 1):
        start = EPOCH_1073_START + (epoch - 1073) * EPOCH_SLOTS
        for t in targets:
            r = rpc(url, 'getLeaderSchedule', [start, dict(identity=t['pubkey'])]).get('result') or {}
            windows = sorted({i // 4 for i in r.get(t['pubkey'], [])})
            slots = [start + w * 4 for w in windows]
            slots = [s for s in slots if ALPENGLOW_SLOT <= s and s + 8 < tip]
            if not slots:
                continue
            # even spread over the epoch, so time of day does not lean one way
            k = min(per_epoch, len(slots)); step = len(slots) / k
            picks = [slots[min(int((i + rng.random()) * step), len(slots) - 1)] for i in range(k)]
            for s in picks:
                for off in OFFSETS:
                    todo.append((epoch, t, s, off))
    rng.shuffle(todo)
    return todo


def main():
    url = os.environ['TIEPAIRS_RPC']
    targets = json.load(open(sys.argv[1])); out = sys.argv[2]
    first, last, per_epoch = (int(x) for x in (sys.argv[3:6] or (1065, 1073, 8)))
    done = set()
    if os.path.exists(out):
        with open(out) as f:
            done = {json.loads(line)['slot'] for line in f if line.strip()}
    tip = rpc(url, 'getSlot', [dict(commitment='finalized')])['result']
    todo = plan(url, targets, first, last, per_epoch, tip)
    print(f'planned {len(todo)} blocks, {len(done)} already stored', flush=True)
    got = skipped = 0; t0 = time.time()
    with open(out, 'a') as f:
        for n, (epoch, t, win, off) in enumerate(todo, 1):
            slot = win + off
            if slot in done:
                continue
            r = rpc(url, 'getBlock', [slot, dict(maxSupportedTransactionVersion=1, transactionDetails='full', rewards=True, commitment='finalized')])
            b = r.get('result')
            rec = dict(slot=slot, epoch=epoch, label=t['label'], pubkey=t['pubkey'], off=off)
            if not b:
                rec.update(skipped=True, err=(r.get('error') or {}).get('code')); skipped += 1
            else:
                vals = []
                for tx in b.get('transactions') or []:
                    try:
                        p, pf = priority(tx)
                        vals.append((round(math.log10(p + 1), 3), 1 if tx['meta'].get('err') else 0, int(str(tx.get('version')) == '1')))
                    except Exception:
                        vals.append(None)
                fee = [x['pubkey'] for x in (b.get('rewards') or []) if x.get('rewardType') == 'Fee']
                rec.update(leader=fee[0] if fee else None, time=b.get('blockTime'), n=len(vals),
                           lg=[v[0] if v else None for v in vals], err=[i for i, v in enumerate(vals) if v and v[1]],
                           v1=sum(v[2] for v in vals if v))
                got += 1
            f.write(json.dumps(rec, separators=(',', ':')) + '\n'); f.flush()
            if n % 100 == 0:
                print(f'{n}/{len(todo)} blocks, stored {got}, skipped {skipped}, {time.time() - t0:.0f}s', flush=True)
            time.sleep(.12)
    print(f'done: stored {got}, skipped {skipped}, {time.time() - t0:.0f}s', flush=True)


if __name__ == '__main__':
    main()
