"""One run: for each planned leader window, put two equal-priority probes in the leader's buffer, 10 ms apart.

The order of work for a pair is the point of this file. The signed bytes reach the disk first, then the worst-case fee
reaches the ledger, and only then may anything leave. A cancelled pair gets its reservation back because this process
knows it never touched a socket; a crash gets nothing back. In a dry run the same timeline executes with real slots,
blockhashes, signatures and QUIC links, and the two send instants are rehearsed without a stream.
"""
import asyncio
import base64
import collections
import hashlib
import os
import time
from dataclasses import dataclass

from . import contrasts
from . import journal as journal_module
from . import tx
from .ledger import CapExceeded
from .slots import SlotTimeout
from .wire import HANDSHAKE_TIMEOUT, ConnectionBudget, LinkDown, StreamBudget

TEMPLATE = tx.Template(cu_limit=6_000, cu_price=1_420_000, loaded_limit=128 * 1024)
MEMO_LEN = 8  # the Memo program charges about 340 CU per byte

# Why a pair never left. Each one is its own field in probes.jsonl.
CANCEL_FIELDS = ('behind_schedule', 'late_anchor', 'stale_clock', 'version_changed', 'no_link', 'connection_budget',
                 'cap', 'slot_timeout', 'send_failed', 'other')


@dataclass
class Config:
    gap_ms: float = 10.0
    anchor_slots: int = 5     # fire when slot F-5 is seen: about 1 s before the first shred of F, at least 0.4 s on polls alone
    cancel_slots: int = 3     # a poll showing F-3 at fire time means the anchor was missed
    window_anchor: int = 0    # inside the window: fire when F itself is first seen
    window_cancel: int = -2   # a poll showing F+2 means half the window is gone
    prepare_slots: int = 24   # sign and journal about 5 s before F
    warm_slots: int = 40      # open the window's connection about 7 s before the pair; pairs are 40 slots apart, so only a window pair can still be firing
    max_clock_age_ms: float = 1_500.0
    gossip_seconds: float = 60.0
    fast_rtt_seconds: float = 0.3
    ack_wait_seconds: float = 0.4
    slot_timeout: float = 90.0


def memo_label(run_tag, seq, role):
    label = f'{run_tag}{seq:04d}{role}'.encode()
    if len(label) != MEMO_LEN:
        raise ValueError(f'memo label {label!r} is not {MEMO_LEN} bytes')
    return label


class Gossip:
    """The newest getClusterNodes, so each window can record what its leader showed moments before."""

    def __init__(self, nodes=()):
        self.taken = 0.0
        self.by_key = {}
        if nodes:
            self.update(nodes)

    def update(self, nodes):
        self.by_key = {n['pubkey']: n for n in nodes}
        self.taken = time.time()

    def view(self, identity):
        n = self.by_key.get(identity) or {}
        return dict(client=n.get('clientId') or '', version=n.get('version') or '', tpu_quic=n.get('tpuQuic'),
                    age_s=round(time.time() - self.taken, 1))


class PreflightFailed(Exception):
    pass


def check_shape(rpc, payer, message, template, data_bytes):
    """simulateTransaction without a signature and getFeeForMessage against the plan. Nothing is broadcast, nothing costs
    anything, and any difference stops the run."""
    reply = rpc.simulate(tx.unsigned_wire(message))
    fee = rpc.call('getFeeForMessage', [base64.b64encode(message).decode(), dict(commitment='confirmed')])['value']
    row = dict(payer=payer, err=reply.get('err'), units=reply.get('unitsConsumed'), loaded=reply.get('loadedAccountsDataSize'),
               fee=fee, planned_fee=template.total_fee(), cost=template.cost(data_bytes), priority=template.priority(data_bytes))
    problems = []
    if row['err'] is not None:
        problems.append(f'simulation failed: {row["err"]}')
    if (row['units'] or 0) > template.cu_limit:
        problems.append(f'{row["units"]} CU over the limit {template.cu_limit}')
    if (row['loaded'] or 0) > template.loaded_limit:
        problems.append(f'{row["loaded"]} loaded bytes over the limit {template.loaded_limit}')
    if fee != template.total_fee():
        problems.append(f'getFeeForMessage says {fee}, the plan says {template.total_fee()}')
    if problems:
        raise PreflightFailed(f'{payer}: ' + '; '.join(problems))
    return row


def preflight(rpc, template, payers):
    """Block T's probe shape, before anything is planned, in dry runs too."""
    memo = memo_label('chk', 1, 'a')
    data_bytes = tx.probe_data_bytes(len(memo))
    blockhash = rpc.blockhash()['blockhash']
    lock = tx.lock_address('preflight')
    return [check_shape(rpc, payer, tx.probe_message(tx.key_bytes(payer), lock, blockhash, template, memo), template, data_bytes)
            for payer in payers]


def preflight_kinds(rpc, payers):
    """Every template of block P on every payer."""
    blockhash = rpc.blockhash()['blockhash']
    lock = tx.lock_address('preflight')
    cosigner = tx.Keypair.generate().public
    def message(payer, template):
        if template.version == 1:
            return tx.v1_message(tx.key_bytes(payer), lock, blockhash, template)
        return tx.bare_message(tx.key_bytes(payer), lock, blockhash, template, cosigner if template.signatures == 2 else None)
    return [check_shape(rpc, payer, message(payer, template), template, contrasts.data_bytes(template))
            for payer in payers for template in contrasts.templates()]


def templates_of(pair, template=TEMPLATE):
    """role -> template. Block T sends one template from both payers."""
    if not pair.kind:
        return dict(a=template, b=template)
    kind = contrasts.KINDS[pair.kind]
    return {pair.x_role: kind.x, 'a' if pair.x_role == 'b' else 'b': kind.y}


def planned_fees(plan, template=TEMPLATE):
    """Lamports each payer pays if everything lands."""
    fees = dict(a=0, b=0)
    for pair in plan:
        for role, t in templates_of(pair, template).items():
            fees[role] += t.total_fee()
    return fees


class Cancelled(Exception):
    def __init__(self, field, detail=''):
        super().__init__(field)
        self.field = field
        self.detail = detail


class Session:
    def __init__(self, cfg, rpc, watcher, pool, ledger, payers, run_dir, run_id, auth, gossip, template=TEMPLATE, address_of=None):
        self.cfg = cfg
        self.rpc = rpc
        self.watcher = watcher
        self.pool = pool
        self.ledger = ledger
        self.payers = payers
        self.run_dir = run_dir
        self.run_id = run_id
        self.run_tag = run_id[-3:]
        self.auth = auth
        self.live = auth is not None
        self.gossip = gossip
        self.template = template
        self.address_of = address_of or {}
        self.cosigner = tx.Keypair.generate()  # the second signature of S: it holds nothing and writes nothing, so it need not outlive the run
        self.journal = journal_module.Journal(os.path.join(run_dir, 'journal.jsonl'))
        self.probes = journal_module.Journal(os.path.join(run_dir, 'probes.jsonl'))
        self.counts = collections.Counter()

    async def _call(self, method, *args):
        return await asyncio.to_thread(method, *args)

    def _timeout(self, slot):
        return max(5.0, (slot - self.watcher.latest) * 0.5 + self.cfg.slot_timeout)

    async def run_pair(self, pair, planned):
        """planned: targets.Checked taken when the plan was made."""
        cfg = self.cfg
        F = pair.first_slot
        anchor, too_late = (cfg.anchor_slots, cfg.cancel_slots) if pair.regime == contrasts.BUFFERED else (cfg.window_anchor, cfg.window_cancel)
        record = dict(pair.asdict(), run=self.run_id, block=self.ledger.block, live=self.live, status='cancelled', lock=tx.lock_address(f'{self.run_id}:{pair.seq}'),
                      no_window=self.auth is not None and self.auth.network == 'localnet',
                      gossip_plan=dict(client=planned.client, version=planned.version, tpu_quic=planned.tpu_quic),
                      addr=self.address_of.get(pair.identity) or planned.tpu_quic)
        reserved = []
        try:
            await self.watcher.wait(F - cfg.warm_slots, self._timeout(F - cfg.warm_slots))
            await self._warm(record)
            await self.watcher.wait(F - cfg.prepare_slots, self._timeout(F - cfg.prepare_slots))
            view = self.gossip.view(pair.identity)
            record['gossip_window'] = view
            if view['client'] != planned.client or view['version'] != planned.version:
                raise Cancelled('version_changed', f'shows {view["client"]} {view["version"]}')
            if view['tpu_quic'] and pair.identity not in self.address_of:  # a leader that moved is still the same leader
                record['addr'] = view['tpu_quic']
            if self.watcher.latest >= F - anchor - 4:
                raise Cancelled('behind_schedule', f'slot {self.watcher.latest}')
            latest = await self._call(self.rpc.blockhash)
            templates = templates_of(pair, self.template)
            prepared = self.prepare(pair, record, latest, templates)
            self.journal.write(prepared)
            for role, signature in (('a', record['sig_a']), ('b', record['sig_b'])):
                self.ledger.reserve(templates[role].total_fee(), 'probe', signature)
                reserved.append(signature)
            await self.watcher.wait(F - anchor, self._timeout(F - anchor))
            if self.watcher.latest >= F - too_late:
                raise Cancelled('late_anchor', f'slot {self.watcher.latest}')
            if self.watcher.age_ms() > cfg.max_clock_age_ms:
                raise Cancelled('stale_clock', f'last poll {self.watcher.age_ms():.0f} ms ago')
            link = await self._link(record)  # the window's connection, or a new one if the server has dropped it
            if self.watcher.latest >= F - too_late:
                raise Cancelled('late_anchor', f'slot {self.watcher.latest} after connecting')
            record.update(polled_slot_at_fire=self.watcher.latest, poll_age_ms=round(self.watcher.age_ms(), 1))
            record['fired'] = True  # from here on a failure can no longer prove that the bytes stayed home
            await self.fire(link, pair, record, reserved)
            return record
        except Cancelled as e:
            return self.cancel(record, reserved, e.field, e.detail)
        except CapExceeded as e:
            return self.cancel(record, reserved, 'cap', str(e))
        except SlotTimeout as e:
            return self.cancel(record, reserved, 'slot_timeout', str(e))
        except (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError) as e:
            return self.cancel(record, reserved, 'no_link', f'{type(e).__name__}: {e}')
        except ConnectionBudget as e:
            return self.cancel(record, reserved, 'connection_budget', str(e))
        except Exception as e:
            return self.cancel(record, reserved, 'other', f'{type(e).__name__}: {e}')
        finally:
            self.counts[record['status']] += 1
            self.write_probes(record)
            await self.pool.release(record['addr'])

    async def _warm(self, record):
        """Every window gets its own connection, so link age cannot differ between groups: left open, a link to a treated
        validator outlived the spacing while a control's did not. A failure here is not final, the anchor tries again."""
        try:
            await self._link(record, HANDSHAKE_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError, ConnectionBudget) as e:
            record['warm_failed'] = f'{type(e).__name__}: {e}'

    async def _link(self, record, timeout=0.5):
        link, fresh = await self.pool.get(record['addr'], timeout)
        if fresh:
            record['new_links'] = record.get('new_links', 0) + 1
            record['handshake_ms'] = round(link.handshake_ms, 2)
        record.update(conn_id=link.conn_id, local_port=link.local_port, link_age_s=round(time.time() - link.opened_at, 2))
        return link

    def build(self, pair, role, lock, blockhash, template):
        if pair.kind:
            if template.version == 1:
                return tx.build_v1_probe(self.payers[role], lock, blockhash, template)
            return tx.build_bare_probe(self.payers[role], lock, blockhash, template, self.cosigner)
        return tx.build_probe(self.payers[role], lock, blockhash, template, memo_label(self.run_tag, pair.seq, role))

    def prepare(self, pair, record, latest, templates):
        built = {role: self.build(pair, role, record['lock'], latest['blockhash'], templates[role]) for role in ('a', 'b')}
        wire_a, wire_b = tx.wire_priority(built['a'][0]), tx.wire_priority(built['b'][0])
        if templates['a'] == templates['b'] and wire_a != wire_b:
            raise Cancelled('other', 'members of the pair do not tie')
        record.update(sig_a=built['a'][1], sig_b=built['b'][1], blockhash=latest['blockhash'],
                      last_valid=latest.get('lastValidBlockHeight'), priority_a=wire_a['priority'], priority_b=wire_b['priority'],
                      fee_a=wire_a['fee'], fee_b=wire_b['fee'],
                      raw_a=base64.b64encode(built['a'][0]).decode(), raw_b=base64.b64encode(built['b'][0]).decode(),
                      payer_a=self.payers['a'].address, payer_b=self.payers['b'].address, status='prepared')
        return dict(record)

    async def fire(self, link, pair, record, reserved):
        first, second = ('a', 'b') if pair.first_sent == 'a' else ('b', 'a')
        raw = {role: base64.b64decode(record[f'raw_{role}']) for role in ('a', 'b')}
        gap_ms = contrasts.KINDS[pair.kind].gap_ms if pair.kind else self.cfg.gap_ms
        gap_ns = int(gap_ms * 1e6)
        link.sample_fast(self.cfg.fast_rtt_seconds)
        record['rtt_before_ms'] = link.handshake_ms if link.rtt_last is None else link.rtt_last  # a handshake is one RTT
        sent = {}
        try:
            sent[first] = self._write(link, raw[first], record)
        except (LinkDown, StreamBudget) as e:  # both are raised before the stream is touched
            del record['fired']
            raise Cancelled('send_failed', str(e)) from None
        target = sent[first]['t_ns'] + gap_ns  # from when the first write returned: a slow first write must not eat a short gap
        if gap_ms > 3:  # a shorter gap is spun through: nothing else may run between the writes
            await asyncio.sleep(gap_ms / 1000 - 0.003)
        while time.perf_counter_ns() < target:
            pass
        try:
            sent[second] = self._write(link, raw[second], record)
        except (LinkDown, StreamBudget) as e:
            self.ledger.settle(record[f'sig_{first}'])
            self.ledger.release(record[f'sig_{second}'], 'second member never reached a socket')
            record.update(status='partial', reason=f'second member: {e}', **{f'sent_{first}': sent[first]})
            self.journal.write(dict(seq=pair.seq, status='partial', reason=record['reason'], **{f'sent_{first}': sent[first]}))
            return
        for signature in reserved:
            self.ledger.settle(signature)
        status = 'sent' if self.live else 'rehearsed'
        record.update(status=status, gap_ms=round((sent[second]['t_ns'] - sent[first]['t_ns']) / 1e6, 3),
                      sent_a=sent['a'], sent_b=sent['b'])
        self.journal.write(dict(seq=pair.seq, status=status, gap_ms=record['gap_ms'], sent_a=sent['a'], sent_b=sent['b'],
                                conn_id=record.get('conn_id'), link_age_s=record.get('link_age_s'), new_links=record.get('new_links', 0),
                                polled_slot_at_fire=record.get('polled_slot_at_fire')))
        await asyncio.sleep(self.cfg.ack_wait_seconds)
        record['rtt_after_ms'] = link.rtt_last
        for role in ('a', 'b'):
            ack = link.acks.get(sent[role].get('ack_uid'))
            sent[role]['ack_ms'] = round((ack - sent[role]['t_ns']) / 1e6, 3) if ack else None
        self.journal.write(dict(seq=pair.seq, acks=dict(a=sent['a']['ack_ms'], b=sent['b']['ack_ms']),
                                rtt_before_ms=record['rtt_before_ms'], rtt_after_ms=record['rtt_after_ms']))

    def _write(self, link, raw, record):
        if self.live:
            return link.send(raw)
        return dict(t_ns=time.perf_counter_ns(), wall_ns=time.time_ns(), stream_id=None)

    def cancel(self, record, reserved, field, detail):
        """Reservations come back only when no write was attempted. After the first write nothing is proven, so it stays charged."""
        if record.get('fired'):
            for signature in reserved:
                self.ledger.settle(signature)
            record.update(status='send_error', reason=f'{field}: {detail}')
            self.journal.write(dict(seq=record['seq'], status='send_error', reason=record['reason']))
            return record
        for signature in reserved:
            self.ledger.release(signature, field)
        record.update(status='cancelled', cancel=field, reason=detail)
        if 'raw_a' in record:
            self.journal.write(dict(seq=record['seq'], status='cancelled', cancel=field, reason=detail))
        else:
            self.journal.write({k: v for k, v in record.items() if not k.startswith('raw_')})
        return record

    def write_probes(self, record):
        for line in probe_records(record):
            self.probes.write(line)

    async def watch_gossip(self):
        while True:
            await asyncio.sleep(self.cfg.gossip_seconds)
            try:
                self.gossip.update(await self._call(self.rpc.call, 'getClusterNodes'))
            except Exception:
                continue

    async def run(self, plan, planned, on_record=None):
        tasks = []
        gossip_task = asyncio.create_task(self.watch_gossip())
        try:
            for pair in plan:
                start_at = pair.first_slot - self.cfg.prepare_slots - 30
                await self.watcher.wait(start_at, self._timeout(start_at))
                tasks.append(asyncio.create_task(self.run_pair(pair, planned[pair.identity])))
                tasks = self._reap(tasks, on_record)
                if self.ledger.left() < 2 * self.template.total_fee():
                    self.journal.write(dict(event='stopped', reason='headroom below one pair', t=time.time()))
                    break
            while tasks:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                tasks = self._reap(tasks, on_record)
        finally:
            gossip_task.cancel()
            for t in tasks:
                t.cancel()
            self.journal.close()
            self.probes.close()

    def _reap(self, tasks, on_record):
        for t in tasks:
            if t.done() and on_record:
                on_record(t.result())
        return [t for t in tasks if not t.done()]


def probe_records(record):
    """One line per probe: send time, connection, leader address, gossip at plan and at window, and each cancel reason."""
    first_role = record['first_sent']
    second_role = 'a' if first_role == 'b' else 'b'
    lines = []
    for order, role in ((1, first_role), (2, second_role)):
        sent = record.get(f'sent_{role}') or {}
        line = {k: record.get(k) for k in ('run', 'block', 'seq', 'status', 'identity', 'group', 'first_slot', 'first_sent',
                                           'addr', 'conn_id', 'local_port', 'link_age_s', 'new_links', 'handshake_ms',
                                           'gossip_plan', 'gossip_window', 'lock', 'blockhash', 'gap_ms', 'kind', 'x_role', 'regime',
                                           'polled_slot_at_fire', 'poll_age_ms', 'rtt_before_ms', 'rtt_after_ms', 'reason')}
        line.update(role=role, order=order, signature=record.get(f'sig_{role}'), payer=record.get(f'payer_{role}'),
                    priority=record.get(f'priority_{role}'), fee=record.get(f'fee_{role}'),
                    t_send_wall_ns=sent.get('wall_ns'), t_send_mono_ns=sent.get('t_ns'), stream_id=sent.get('stream_id'),
                    ack_ms=sent.get('ack_ms'))
        for field in CANCEL_FIELDS:
            line[f'x_{field}'] = record.get('cancel') == field
        line['x_crash'] = record.get('status', '').startswith('crash')
        lines.append(line)
    return lines


def recover(run_dir, ledger):
    """Before a restarted run plans anything: settle what the last one left, and keep its probe lines complete."""
    path = os.path.join(run_dir, 'journal.jsonl')
    settled = journal_module.recover(path, ledger)
    probes_path = os.path.join(run_dir, 'probes.jsonl')
    logged = {r.get('seq') for r in journal_module.read_records(probes_path)}
    missing = [p for seq, p in sorted(journal_module.merged_pairs(path).items()) if seq not in logged]
    if missing:
        probes = journal_module.Journal(probes_path)
        for record in missing:
            for line in probe_records(record):
                probes.write(line)
        probes.close()
    return settled


def code_digest():
    here = os.path.dirname(__file__)
    digest = hashlib.sha256()
    for name in sorted(os.listdir(here)):
        if name.endswith('.py'):
            with open(os.path.join(here, name), 'rb') as f:
                digest.update(name.encode() + f.read())
    return digest.hexdigest()


def windows_for(rpc, wanted):
    from . import schedule
    epoch = rpc.call('getEpochInfo', [dict(commitment='confirmed')])
    return epoch, {t.identity: schedule.upcoming_windows(rpc, t.identity, epoch) for t in wanted}
