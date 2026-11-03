"""Spend ledger shared by every run and block of the project.

A worst-case fee is reserved and fsynced per transaction before it can leave. A reservation is released only by the
process that made it, and only when that process knows the bytes never reached a socket. Whatever a crash leaves
open stays charged, so a restart can only under-spend. An exclusive file lock keeps a second process out.
"""
import fcntl
import json
import os
import time

TORN_PENALTY = 100_000  # lamports charged for a half-written record: more than any single transaction reserves


class CapExceeded(Exception):
    pass


class LedgerBusy(Exception):
    pass


class Ledger:
    """`cap` limits this block, `project_cap` everything in the file. With path=None it is an in-memory stand-in for dry runs."""

    def __init__(self, path, cap, block='T', project_cap=None, raise_cap=False):
        self.path = path
        self.cap = cap
        self.block = block
        self.project_cap = cap if project_cap is None else project_cap
        self.spent = 0
        self.block_spent = 0
        self.open = {}  # reservations of this process that may still be released
        self.all_refs = set()
        self.f = None
        if path is None:
            return
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        self.lock = open(path + '.lock', 'w')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LedgerBusy(f'{path} is held by another process') from None
        stored = self._load()
        for name, value in ((f'cap:{block}', cap), ('cap:project', self.project_cap)):
            if name in stored and stored[name] != value and not raise_cap:
                self.lock.close()
                raise CapExceeded(f'{name} in the ledger is {stored[name]} lamports, {value} requested: pass --raise-cap to change it')
        self.f = open(path, 'a')
        for name, value in ((f'cap:{block}', cap), ('cap:project', self.project_cap)):
            if stored.get(name) != value:
                self._append(dict(kind='cap', name=name, value=value))

    def _load(self):
        stored = {}
        if not os.path.exists(self.path):
            return stored
        with open(self.path, 'rb') as f:
            blob = f.read()
        lines = blob.split(b'\n')
        tail = lines.pop()  # empty when the last record ended cleanly
        for at, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                if b'"torn"' not in (lines[at + 1] if at + 1 < len(lines) else b''):  # a repair record already charged it
                    self.spent += TORN_PENALTY
                continue
            if record.get('kind') == 'cap':
                stored[record['name']] = record['value']
                continue
            lamports = record.get('lamports', 0)
            self.spent += lamports
            if record.get('block') == self.block:
                self.block_spent += lamports
            if record.get('ref') is not None:
                self.all_refs.add(record['ref'])
        if tail.strip():
            self.spent += TORN_PENALTY
            self.block_spent += TORN_PENALTY
            with open(self.path, 'ab') as f:
                repair = dict(kind='torn', block=self.block, lamports=TORN_PENALTY, t=time.time())
                f.write(b'\n' + json.dumps(repair).encode() + b'\n')
                f.flush()
                os.fsync(f.fileno())
        return stored

    def _append(self, record):
        record.setdefault('t', time.time())
        self.f.write(json.dumps(record) + '\n')
        self.f.flush()
        os.fsync(self.f.fileno())

    def left(self):
        return min(self.cap - self.block_spent, self.project_cap - self.spent)

    def reserve(self, lamports, kind, ref):
        if self.block_spent + lamports > self.cap:
            raise CapExceeded(f'{kind}: block {self.block} would reach {self.block_spent + lamports} of {self.cap} lamports')
        if self.spent + lamports > self.project_cap:
            raise CapExceeded(f'{kind}: project would reach {self.spent + lamports} of {self.project_cap} lamports')
        if self.f:
            self._append(dict(kind=kind, block=self.block, lamports=lamports, ref=ref))
        self.spent += lamports
        self.block_spent += lamports
        self.open[ref] = lamports
        self.all_refs.add(ref)

    def release(self, ref, why):
        """Only for a transaction this process holds and knows it never wrote to a socket."""
        lamports = self.open.pop(ref)
        if self.f:
            self._append(dict(kind='release', block=self.block, lamports=-lamports, ref=ref, why=why))
        self.spent -= lamports
        self.block_spent -= lamports

    def settle(self, ref):
        """The bytes left. From here on no code path can release this reservation."""
        self.open.pop(ref, None)

    def close(self):
        if self.f:
            self.f.close()
            self.lock.close()
            self.f = None
