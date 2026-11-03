#!/usr/bin/env python3
"""Recheck the published numbers from the raw data. No keys and no network: block positions need `python -m tiepairs verify`.

For every run under data/ it checks the file digests in manifest.json, recomputes the coins and the decks from the seed, verifies
every signature and recomputes every fee and priority from the bytes in the journal. Then it recomputes the verdicts from
outcomes.jsonl and compares them with report/, and the frozen digests from the files against the Memo text. It exits non-zero
if anything disagrees. With --chain it also reads the two Memos from a public RPC. Usage: reproduce.py [ROOT] [--chain]
"""
import base64
import collections
import hashlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from tiepairs import contrasts, results, rules, schedule, session, tx  # noqa: E402
from tiepairs.rpc import Rpc  # noqa: E402


def digest(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def tree_digest(directory):
    """The digest session.code_digest gives, for any copy of the package."""
    total = hashlib.sha256()
    for name in sorted(os.listdir(directory)):
        if name.endswith('.py'):
            with open(os.path.join(directory, name), 'rb') as f:
                total.update(name.encode() + f.read())
    return total.hexdigest()


def check_memos(root, chain):
    """Each Memo holds three digests. They must come out of the files in this repository, and with --chain the text must be on chain."""
    problems = []
    with open(os.path.join(root, 'data', 'memos.json')) as f:
        memos = json.load(f)
    for memo in memos:
        name = os.path.splitext(os.path.basename(memo['hypotheses']))[0]
        text = (f'tiepairs {name} sha256:{digest(os.path.join(root, memo["hypotheses"]))} code:{tree_digest(os.path.join(root, memo["code"]))} '
                f'targets:{digest(os.path.join(root, memo["targets"]))}')
        if text != memo['text']:
            problems.append(f'block {memo["block"]}: the files give {text}')
        if chain:
            reply = Rpc(pause=0.3).call('getTransaction', [memo['signature'], dict(encoding='json', maxSupportedTransactionVersion=1, commitment='confirmed')])
            if tx.b58decode(reply['transaction']['message']['instructions'][0]['data']).decode() != memo['text'] or reply['slot'] != memo['slot']:
                problems.append(f'block {memo["block"]}: the chain holds another Memo')
        print(f'block {memo["block"]}: Memo in slot {memo["slot"]} matches the files{" and the chain" if chain else ""}: {not problems}')
    return problems


def check_manifest(run_dir):
    with open(os.path.join(run_dir, 'manifest.json')) as f:
        listed = json.load(f)
    bad = [name for name, value in listed.items() if digest(os.path.join(run_dir, name)) != value]
    return len(listed), bad


def wire_problems(pair):
    """Everything one pair's journal record promises about its own bytes."""
    problems = []
    raw = {role: base64.b64decode(pair[f'raw_{role}']) for role in ('a', 'b')}
    for role, data in raw.items():
        tx.verify_signature(data)
        if tx.b58encode(tx.decode(data)['signatures'][0]) != pair[f'sig_{role}']:
            problems.append(f'#{pair["seq"]}: the journal signature of {role} is not in its bytes')
    wire = {role: tx.wire_priority(data) for role, data in raw.items()}
    planned = contrasts.KINDS.get(pair.get('kind'))
    if not planned:
        return problems + ([f'#{pair["seq"]}: the pair does not tie'] if wire['a'] != wire['b'] else [])
    other = 'a' if pair['x_role'] == 'b' else 'b'
    for role, template in ((pair['x_role'], planned.x), (other, planned.y)):
        if (wire[role]['fee'], wire[role]['priority']) != (template.total_fee(), template.priority(contrasts.data_bytes(template))):
            problems.append(f'#{pair["seq"]}: {role} does not carry the template of {pair["kind"]}')
    return problems


def check_run(block, run_dir, meta):
    seed = meta['hypotheses_sha256']
    pairs = results.load_pairs(run_dir)
    problems = []
    counts = collections.Counter(pairs=len(pairs))
    for pair in pairs:
        if schedule.coin(seed, pair['first_slot'], pair['identity']) != pair['first_sent']:
            problems.append(f'#{pair["seq"]}: send order does not follow the seed')
        if pair.get('kind') and schedule.x_role(seed, pair['first_slot'], pair['identity'], pair['kind']) != pair['x_role']:
            problems.append(f'#{pair["seq"]}: which payer carries x does not follow the seed')
        counts['coins'] += 1
        if pair.get('raw_a') and pair.get('raw_b'):
            problems += wire_problems(pair)
            counts['signed pairs'] += 1
    if block != 'T':
        identities = {p['identity'] for p in pairs}
        for identity in identities:
            cards = collections.Counter(contrasts.deck(seed, meta['session'], identity))
            sent = collections.Counter(p['kind'] for p in pairs if p['identity'] == identity)
            extra = sent - cards
            if extra:
                problems.append(f'{identity[:8]}: pairs outside its deck: {dict(extra)}')
    return counts, problems


def compare(ours, theirs, path=''):
    """Paths where two reports differ. Floats are compared closely: a report goes through JSON and back."""
    if isinstance(theirs, dict):
        out = []
        for key in sorted(set(ours) | set(theirs)):
            if key not in ours or key not in theirs:
                out.append(f'{path}/{key}')
            else:
                out += compare(ours[key], theirs[key], f'{path}/{key}')
        return out
    if isinstance(theirs, float) or isinstance(ours, float):
        return [] if ours == theirs or abs(ours - theirs) < 1e-9 else [path]
    return [] if ours == theirs else [path]


def check_report(block, run_dirs, committed):
    with tempfile.TemporaryDirectory() as out:
        work = []
        for run_dir in run_dirs:  # write_report refreshes manifests, so it works on a copy of what it reads
            copy = os.path.join(out, os.path.basename(run_dir))
            os.makedirs(copy)
            for name in ('outcomes.jsonl', 'rtt.jsonl', 'meta.json'):
                source = os.path.join(run_dir, name)
                if os.path.exists(source):
                    with open(source, 'rb') as a, open(os.path.join(copy, name), 'wb') as b:
                        b.write(a.read())
            work.append(copy)
        report, text = (results if block == 'T' else rules).write_report(work, os.path.join(out, 'report'))
        fresh = json.loads(json.dumps(report))
    with open(os.path.join(committed, 'summary.json')) as f:
        return compare(fresh, json.load(f)), text


def main():
    chain = '--chain' in sys.argv
    paths = [a for a in sys.argv[1:] if a != '--chain']
    root = paths[0] if paths else HERE
    data = os.path.join(root, 'data')
    print(f'code digest of this tree: {session.code_digest()}')
    problems = check_memos(root, chain)
    for line in problems:
        print(f'  {line}')
    failed = bool(problems)
    for block in sorted(d for d in os.listdir(data) if os.path.isdir(os.path.join(data, d))):
        block_dir = os.path.join(data, block)
        run_dirs = sorted(os.path.join(block_dir, name) for name in os.listdir(block_dir) if os.path.exists(os.path.join(block_dir, name, 'meta.json')))
        if not run_dirs:  # data/coverage holds RPC snapshots, not runs
            continue
        for run_dir in run_dirs:
            with open(os.path.join(run_dir, 'meta.json')) as f:
                meta = json.load(f)
            files, bad = check_manifest(run_dir)
            counts, problems = check_run(block, run_dir, meta)
            print(f'{block} {os.path.basename(run_dir)}: {files} files, {dict(counts)}, code {meta["code_sha256"][:12]}, hypotheses {meta["hypotheses_sha256"][:12]}')
            for line in [f'digest differs: {name}' for name in bad] + problems:
                print(f'  {line}')
                failed = True
        differs, text = check_report(block, run_dirs, os.path.join(root, 'report', block))
        if differs:
            print(f'{block}: the recomputed report differs at {differs[:10]}')
            failed = True
        else:
            print(f'{block}: the recomputed report equals report/{block}/summary.json\n')
            print(text)
    sys.exit('Something does not reproduce.' if failed else 0)


if __name__ == '__main__':
    main()
