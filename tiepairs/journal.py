"""Write-ahead journal of a run: signed bytes reach the disk before they can reach a socket.

Each pair moves through `prepared` -> `sent` | `partial` | `cancelled`. A pair left at `prepared` by a power cut is settled
on restart as `crash_unsent` (no reservation, so it cannot have left) or `crash_unknown` (it may have left: never resent,
stays charged, and its signatures are still looked up on chain).
"""
import json
import os

UNFINISHED = 'prepared'
SPENT_STATUSES = ('sent', 'partial', 'send_error', 'crash_unknown')


class Journal:
    def __init__(self, path):
        self.path = path
        created = not os.path.exists(path)
        torn = False
        if not created and os.path.getsize(path):
            with open(path, 'rb') as f:
                f.seek(-1, os.SEEK_END)
                torn = f.read(1) != b'\n'
        self.f = open(path, 'a')
        if torn:  # a power cut left half a line: the next record must not glue onto it
            self.f.write('\n')
        if created:  # the new directory entry has to survive the power cut too
            fd = os.open(os.path.dirname(path) or '.', os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)

    def write(self, record):
        self.f.write(json.dumps(record) + '\n')
        self.f.flush()
        os.fsync(self.f.fileno())

    def close(self):
        self.f.close()


def read_records(path):
    """Tolerates a torn last line: a record that did not fully reach the disk was never acted on."""
    if not os.path.exists(path):
        return []
    records = []
    with open(path) as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records


def merged_pairs(path):
    """seq -> everything known about the pair, later records overriding earlier ones."""
    pairs = {}
    for record in read_records(path):
        if 'seq' in record:
            pairs.setdefault(record['seq'], {}).update(record)
    return pairs


def recover(path, ledger):
    """Settle what a crash left at `prepared`. Nothing here touches the network."""
    settled = []
    pairs = merged_pairs(path)
    journal = Journal(path)
    for seq, pair in sorted(pairs.items()):
        if pair.get('status') != UNFINISHED:
            continue
        refs = [pair.get('sig_a'), pair.get('sig_b')]
        charged = [r for r in refs if r in ledger.all_refs]
        status = 'crash_unknown' if charged else 'crash_unsent'
        update = dict(seq=seq, status=status, reason='power cut before the send was recorded')
        journal.write(update)
        settled.append(dict(pair, **update))
    journal.close()
    return settled


def last_windows(path):
    """Each target's latest window in this run."""
    last = {}
    for pair in merged_pairs(path).values():
        if 'identity' in pair:
            last[pair['identity']] = max(last.get(pair['identity'], 0), pair['first_slot'])
    return last


def spent_kinds(path):
    """target -> kind -> pairs that count as spent, so a restart deals only what is left of a deck."""
    out = {}
    for pair in merged_pairs(path).values():
        if pair.get('status') in SPENT_STATUSES:
            kinds = out.setdefault(pair['identity'], {})
            kinds[pair.get('kind') or ''] = kinds.get(pair.get('kind') or '', 0) + 1
    return out


def progress(path):
    """Pairs that count as spent per target, and the next free sequence number."""
    done = {}
    next_seq = 0
    for seq, pair in merged_pairs(path).items():
        next_seq = max(next_seq, seq + 1)
        if pair.get('status') in SPENT_STATUSES:
            done[pair['identity']] = done.get(pair['identity'], 0) + 1
    return done, next_seq
