"""Commands. Everything is read-only or a dry run unless --live is given, and only `fund`, `prereg`, `run`, `sweep` have it."""
import argparse
import asyncio
import datetime
import hashlib
import json
import os
import random
import secrets as stdlib_secrets
import sys
import time
import urllib.parse

from . import journal, results, schedule, session, targets, tx
from .guard import Refused, authorize
from .ledger import CapExceeded, Ledger, LedgerBusy
from .rpc import PUBLIC_RPC, Rpc, ws_url
from .slots import SlotPoller, SlotWatcher, StampFeed, read_stamps
from .wire import LinkPool, Wire

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RENT_EXEMPT = 890_880
HANDY_FUNDED = 'HEL1USMZKAL2odpNBj2oCjffnFGaYwmbGmyewGv1e2TU'  # any funded account works for a signature-free simulation


def sol(lamports):
    return f'{lamports / tx.LAMPORTS_PER_SOL:.6f} SOL'


def key_path(args, name):
    return os.path.join(args.secrets, f'{name}.json')


def load_key(args, name):
    path = key_path(args, name)
    if not os.path.exists(path):
        sys.exit(f'{path} is missing: run `python -m tiepairs keygen` first')
    return tx.Keypair.load(path)


def ledger_for(args, rpc):
    """One file per cluster, shared by every run and block of the project."""
    path = os.path.join(args.out, f'ledger-{rpc.genesis()[:8]}.jsonl')
    return Ledger(path, int(args.cap_sol * tx.LAMPORTS_PER_SOL), block=args.block,
                  project_cap=int(args.project_cap_sol * tx.LAMPORTS_PER_SOL), raise_cap=args.raise_cap)


def hypotheses_digest(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


FROZEN_GAP_MS = 10.0
FROZEN_PAIRS = 30


def cmd_keygen(args):
    os.makedirs(args.secrets, mode=0o700, exist_ok=True)
    for name in ('work', 'payer-a', 'payer-b'):
        path = key_path(args, name)
        if os.path.exists(path):
            print(f'{name}: {tx.Keypair.load(path).address} (already there)')
            continue
        pair = tx.Keypair.generate()
        pair.dump(path)
        print(f'{name}: {pair.address}')
    print('Поповнюй лише work. Файли в secrets/ не потрапляють у git і в образ.')


def cmd_balances(args):
    rpc = Rpc(args.rpc)
    for name in ('work', 'payer-a', 'payer-b'):
        pair = load_key(args, name)
        print(f'{name:8} {pair.address}  {sol(rpc.balance(pair.address))}')


def checked_targets(args, rpc):
    nodes = rpc.call('getClusterNodes')
    if args.localnet:  # a one-node cluster: its only validator is the target, whatever version the image holds
        wanted = [targets.Target(n['pubkey'], args.localnet_group, n.get('clientId') or '', n.get('version') or '') for n in nodes]
    else:
        wanted = targets.load(args.targets)
    return wanted, targets.verify(wanted, nodes), nodes


def print_checked(checked):
    print(f'{"ціль":10}{"група":10}{"очікую":20}{"бачу":20}{"tpuQuic":24}стан')
    for c in checked:
        print(f'{c.target.short:10}{c.target.group:10}{c.target.client + " " + c.target.version:20}'
              f'{c.client + " " + c.version:20}{c.tpu_quic or "-":24}{"ok" if c.ok else c.reason}')


def cmd_targets(args):
    print_checked(checked_targets(args, Rpc(args.rpc))[1])


def cmd_plan(args):
    rpc = Rpc(args.rpc, pause=0.2)
    _, checked, _ = checked_targets(args, rpc)
    print_checked(checked)
    good = [c.target for c in checked if c.ok]
    epoch, windows = session.windows_for(rpc, good)
    now = epoch['absoluteSlot']
    plan = schedule.plan_pairs(windows, good, args.pairs, now + 60, 'plan-preview')
    per_target = {}
    for p in plan:
        per_target.setdefault(p.identity, []).append(p.first_slot)
    print(f'\nслот {now}, епоха {epoch["epoch"]}, вікон на {args.pairs} пар кожній цілі: {len(plan)}')
    for t in good:
        slots = per_target.get(t.identity, [])
        if slots:
            eta = (slots[-1] - now) * 0.212 / 60
            print(f'{t.short} {t.group:8} перше вікно {slots[0]} (за {(slots[0] - now) * 0.212:.0f} с), останнє за {eta:.0f} хв, вікон {len(slots)}')
        else:
            print(f'{t.short} {t.group:8} вікон попереду немає')
    fee = 2 * session.TEMPLATE.total_fee()
    print(f'\nЦіна пари {fee} лампортів, усього {sol(fee * len(plan))}')


def print_preflight(rows):
    for r in rows:
        print(f'preflight {r["payer"][:8]}: err={r["err"]} units={r["units"]} loaded={r["loaded"]} fee={r["fee"]} '
              f'(план {r["planned_fee"]}) cost={r["cost"]} priority={r["priority"]}')


def cmd_calibrate(args):
    """Simulate one probe with a funded public account as payer: no signature, nothing is sent."""
    print_preflight(session.preflight(Rpc(args.rpc), session.TEMPLATE, [args.payer]))


def announce(args, ledger, lines):
    """Every live command says what it is about to do, then gives a moment to stop it."""
    for line in lines:
        print(line)
    print(f'Ліміт блоку {args.block}: {sol(ledger.cap)}, витрачено {sol(ledger.block_spent)}. '
          f'Ліміт проєкту: {sol(ledger.project_cap)}, витрачено {sol(ledger.spent)}. Лишилось {sol(ledger.left())}.', flush=True)
    if not args.localnet:
        print('Ctrl-C протягом 5 с скасовує.', flush=True)
        time.sleep(5)


def submit(rpc, ledger, auth, raw, signature, fee, kind):
    ledger.reserve(fee, kind, signature)
    try:
        rpc.send(raw, auth)
    finally:
        ledger.settle(signature)
    status = rpc.wait_landed(signature)
    if not status:
        sys.exit(f'{kind}: {signature} не підтвердилась за 60 с, перевір перед повтором')
    if status.get('err'):
        sys.exit(f'{kind}: {signature} впала: {status["err"]}')
    print(f'{kind}: {signature}')


def cmd_fund(args):
    rpc = Rpc(args.rpc)
    work = load_key(args, 'work')
    each = args.each_lamports
    balance = rpc.balance(work.address)
    print(f'work {work.address}: {sol(balance)}; кожному платнику {sol(each)}')
    if each < RENT_EXEMPT + 2 * session.TEMPLATE.total_fee():
        sys.exit(f'замало: платник має лишитись вище rent-exempt {RENT_EXEMPT} лампортів')
    if balance < 2 * each + 2 * tx.LAMPORTS_PER_SIGNATURE + RENT_EXEMPT:
        sys.exit('на work замало коштів')
    if not args.live:
        print('dry run: нічого не надіслано. Додай --live.')
        return
    ledger = ledger_for(args, rpc)
    auth = authorize(True, rpc, ledger, [work], args.localnet)
    payers = {name: load_key(args, name).address for name in ('payer-a', 'payer-b')}
    announce(args, ledger, [f'fund ({auth.network}): {sol(2 * each)} з work {work.address}'] +
             [f'  {sol(each)} -> {name} {address}' for name, address in payers.items()] +
             [f'  комісії {sol(2 * tx.LAMPORTS_PER_SIGNATURE)}'])
    for name in ('payer-a', 'payer-b'):
        raw, signature = tx.build_transfer(work, payers[name], each, rpc.blockhash()['blockhash'])
        submit(rpc, ledger, auth, raw, signature, tx.LAMPORTS_PER_SIGNATURE, 'fund')


def frozen_digests(args):
    return dict(sha256=hypotheses_digest(args.hypotheses), code=session.code_digest(), targets=hypotheses_digest(args.targets))


def cmd_prereg(args):
    frozen = frozen_digests(args)
    memo = f'tiepairs H5 sha256:{frozen["sha256"]} code:{frozen["code"]} targets:{frozen["targets"]}'.encode()
    print(f'{args.hypotheses} sha256 {frozen["sha256"]}')
    if not args.live:
        print(f'dry run: Memo «{memo.decode()}» не надіслано. Додай --live.')
        return
    rpc = Rpc(args.rpc)
    work = load_key(args, 'work')
    ledger = ledger_for(args, rpc)
    auth = authorize(True, rpc, ledger, [work], args.localnet)
    announce(args, ledger, [f'prereg ({auth.network}): Memo від {work.address}, комісія {sol(tx.LAMPORTS_PER_SIGNATURE)}', f'  {memo.decode()}'])
    raw, signature = tx.build_memo(work, memo, rpc.blockhash()['blockhash'])
    submit(rpc, ledger, auth, raw, signature, tx.LAMPORTS_PER_SIGNATURE, 'prereg')
    with open(os.path.join(args.out, f'prereg-{rpc.genesis()[:8]}.json'), 'w') as f:
        json.dump(dict(frozen, signature=signature, t=time.time()), f)


def new_run_id(live):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return f'{stamp}-{"live" if live else "dry"}-{stdlib_secrets.token_hex(2)[:3]}x'


def quantiles(values):
    values = sorted(values)
    if not values:
        return dict(n=0)
    at = lambda q: values[min(len(values) - 1, int(q * len(values)))]  # noqa: E731
    return dict(n=len(values), min=values[0], median=at(0.5), p90=at(0.9), p99=at(0.99), max=values[-1])


def print_timing(records, run_dir):
    stamps = read_stamps(os.path.join(run_dir, 'stamps.jsonl'))
    print('\nЦіль      група     пар  статуси')
    by = {}
    for r in records:
        by.setdefault((r['group'], r['identity']), []).append(r)
    for (group, identity), rows in sorted(by.items()):
        statuses = {}
        for r in rows:
            key = r['status'] if r['status'] != 'cancelled' else f'cancelled:{r.get("cancel")}'
            statuses[key] = statuses.get(key, 0) + 1
        print(f'{identity[:8]:10}{group:10}{len(rows):<5}{statuses}')
    gaps = quantiles([r['gap_ms'] for r in records if r.get('gap_ms') is not None])
    leads = quantiles([x for x in (results.lead_ms(r, stamps[0]) for r in records) if x is not None])
    if gaps['n']:
        print(f'\nЗазор у парі, мс: {gaps}')
    print(f'Випередження першого шреда F за штампом RPC, мінус RTT/2, мс: {leads}')
    print(f'Межа зсуву годинників (мін. затримка штампа), мс: {stamps[1]}')
    network = results.rtt_report(run_dir)
    if network:
        print('\nАдреса                     n     RTT мін/p50/p99/макс, мс        близьких пінгів  повільніше на ≥10 мс')
        for addr, r in network.items():
            print(f'{addr:26}{r["n"]:<6}{r["min"]:.1f}/{r["p50"]:.1f}/{r["p99"]:.1f}/{r["max"]:.1f}'.ljust(66) + f'{r["close_pairs"]:<17}{r["slower_by_gap"]}')


def resume_path(args, rpc):
    return os.path.join(args.out, f'current-{rpc.genesis()[:8]}.txt')


def cmd_run(args):
    rpc = Rpc(args.rpc, pause=0.05)
    digest = hypotheses_digest(args.hypotheses)
    _, checked, nodes = checked_targets(args, rpc)
    print_checked(checked)
    auth = None
    if args.live:
        payers = dict(a=load_key(args, 'payer-a'), b=load_key(args, 'payer-b'))
        ledger = ledger_for(args, rpc)
        auth = authorize(True, rpc, ledger, list(payers.values()), args.localnet)
        if auth.network == 'mainnet':
            prereg_path = os.path.join(args.out, f'prereg-{rpc.genesis()[:8]}.json')
            if not os.path.exists(prereg_path):
                sys.exit('спершу `prereg --live`: хеш гіпотез має бути в ланцюгу до першої проби')
            with open(prereg_path) as f:
                recorded = json.load(f)
            for name, value in frozen_digests(args).items():
                if recorded.get(name) != value:
                    sys.exit(f'{name} змінився після prereg')
            if args.gap_ms != FROZEN_GAP_MS or args.anchor_slots != session.Config.anchor_slots or args.pairs > FROZEN_PAIRS:
                sys.exit('зазор, якір і кількість пар заморожені в H5')
    else:
        payers = dict(a=tx.Keypair.generate(), b=tx.Keypair.generate())
        ledger = Ledger(None, int(args.cap_sol * tx.LAMPORTS_PER_SOL))
    good = [c for c in checked if c.ok]
    if not good:
        sys.exit('жодна ціль не пройшла перевірку')
    print_preflight(session.preflight(rpc, session.TEMPLATE, [p.address for p in payers.values()] if args.live else [args.preflight_payer]))
    run_id = args.run_id
    if args.resume:
        with open(resume_path(args, rpc)) as f:
            run_id = f.read().strip()
    elif args.live and not run_id and os.path.exists(resume_path(args, rpc)):
        with open(resume_path(args, rpc)) as f:
            last = os.path.join(args.out, f.read().strip(), 'journal.jsonl')
        if any(p.get('status') == journal.UNFINISHED for p in journal.merged_pairs(last).values()):
            sys.exit('останній запуск обірвався посеред пари: повтори команду з --resume')
    run_id = run_id or new_run_id(args.live)
    run_dir = os.path.join(args.out, run_id)
    os.makedirs(run_dir, exist_ok=True)
    if args.live:
        with open(resume_path(args, rpc), 'w') as f:
            f.write(run_id)
        settled = session.recover(run_dir, ledger)
        for pair in settled:
            print(f'відновлення: #{pair["seq"]} {pair["status"]}')
    journal_path = os.path.join(run_dir, 'journal.jsonl')
    done, start_seq = journal.progress(journal_path)
    pairs = args.pairs if args.live else args.windows
    cfg = session.Config(gap_ms=args.gap_ms, anchor_slots=args.anchor_slots)
    template = session.TEMPLATE
    good_targets = [c.target for c in good]
    if args.localnet:  # a lone validator leads every slot, so pick windows out of the air: it stays in-slot, not buffered
        now = rpc.call('getSlot')
        windows = {t.identity: [now + 120 + 80 * i for i in range(pairs + 1)] for t in good_targets}
        epoch = dict(absoluteSlot=now)
    else:
        epoch, windows = session.windows_for(rpc, good_targets)
    plan = schedule.plan_pairs(windows, good_targets, pairs, epoch['absoluteSlot'] + cfg.prepare_slots + 40, digest,
                               done=done, start_seq=start_seq, earlier=journal.last_windows(journal_path))
    with open(os.path.join(run_dir, 'plan.jsonl'), 'a') as f:
        for p in plan:
            f.write(json.dumps(p.asdict()) + '\n')
    with open(os.path.join(run_dir, 'meta.json'), 'w') as f:
        json.dump(dict(run=run_id, live=args.live, network=auth.network if auth else 'read-only', hypotheses_sha256=digest,
                       code_sha256=session.code_digest(), template=vars(template), gap_ms=cfg.gap_ms, anchor_slots=cfg.anchor_slots,
                       lock_seed=f'{run_id}:<seq>', payers={k: v.address for k, v in payers.items()}, rpc=urllib.parse.urlsplit(args.rpc).hostname,
                       targets=[dict(identity=c.target.identity, group=c.target.group, client=c.client, version=c.version,
                                     tpu_quic=c.tpu_quic, ok=c.ok, reason=c.reason) for c in checked]), f, indent=1)
    total = len(plan) * 2 * template.total_fee()
    print(f'\n{"LIVE " + auth.network if auth else "DRY RUN"} {run_id}: пар {len(plan)}, комісії до {sol(total)}')
    if auth:
        announce(args, ledger, [f'  платники {payers["a"].address}, {payers["b"].address}'] +
                 [f'  {c.target.short} {c.target.group} {c.client} {c.version} {c.tpu_quic}' for c in good])

    records = []

    def on_record(r):
        records.append(r)
        print(f'#{r["seq"]:<4} {r["identity"][:8]} {r["group"]:8} F={r["first_slot"]} {r["status"]:10} '
              f'{r.get("cancel") or ""} {r.get("reason") or ""} gap={r.get("gap_ms")} rtt={r.get("rtt_before_ms")} '
              f'conn={r.get("conn_id")} ack={[(r.get(k) or {}).get("ack_ms") for k in ("sent_a", "sent_b")]}', flush=True)

    async def go():
        watcher = SlotWatcher()
        poller = SlotPoller(args.rpc, watcher, asyncio.get_running_loop())
        poller.start()
        stamps = StampFeed(args.ws or ws_url(args.rpc), os.path.join(run_dir, 'stamps.jsonl'), watcher)
        stamps.start()
        await watcher.wait(1, 15)
        pool = LinkPool(Wire(auth=auth), os.path.join(run_dir, 'rtt.jsonl'), os.path.join(run_dir, 'links.jsonl'))
        s = session.Session(cfg, rpc, watcher, pool, ledger, payers, run_dir, run_id, auth, session.Gossip(nodes), template,
                            address_of=json.loads(args.address_map) if args.address_map else None)
        try:
            await s.run(plan, {c.target.identity: c for c in good}, on_record)
        finally:
            poller.stop()
            await pool.close()
            await stamps.stop(linger=args.stamp_linger)
        latencies = sorted(poller.latencies)
        print(f'\ngetSlot: опитувань {poller.polls}, помилок {poller.errors}, затримка p50 {latencies[len(latencies) // 2] * 1000:.0f} мс, '
              f'макс {latencies[-1] * 1000:.0f} мс. Витіснень з\'єднань: {dict(pool.evictions)}. Штампів слотів: {stamps.count}.')

    asyncio.run(go())
    print_timing(records, run_dir)
    print(f'\nЖурнали: {run_dir}/journal.jsonl, probes.jsonl, rtt.jsonl, links.jsonl, stamps.jsonl')
    if not args.live:
        print('Dry run: жодна транзакція не вийшла за межі процесу.')


def cmd_results(args):
    rpc = Rpc(args.rpc, pause=0.15)
    outcomes = results.collect(rpc, args.run)
    kinds = {}
    for o in outcomes:
        kinds[o['kind']] = kinds.get(o['kind'], 0) + 1
    print(f'{args.run}: {len(outcomes)} пар, класи {kinds}')


def cmd_analyze(args):
    out_dir = args.report or (args.runs[0] if len(args.runs) == 1 else os.path.join(args.out, 'report'))
    _, text = results.write_report(args.runs, out_dir)
    print(text)


def cmd_verify(args):
    checked, problems = results.verify(Rpc(args.rpc, pause=0.15), args.run, hypotheses_digest(args.hypotheses))
    print(f'перевірено: {checked}')
    for p in problems:
        print(' ', p)
    if problems:
        sys.exit(f'розбіжностей: {len(problems)}')
    print('Розбіжностей немає.')


def cmd_airdrop(args):
    """Localnet only: a cluster that is not mainnet hands out test SOL."""
    rpc = Rpc(args.rpc)
    if rpc.is_mainnet():
        sys.exit('airdrop існує лише на localnet')
    for name in ('work', 'payer-a', 'payer-b'):
        pair = load_key(args, name)
        rpc.wait_landed(rpc.call('requestAirdrop', [pair.address, args.lamports]))
        print(f'{name}: {sol(rpc.balance(pair.address))}')


def cmd_burst(args):
    """Localnet only: equal-priority probes on one lock, written in one go, so the scheduler sees them queued together.
    Measures the tie-break itself: does block order follow send order?"""
    rpc = Rpc(args.rpc, pause=0.02)
    if rpc.is_mainnet():
        sys.exit('burst існує лише на localnet')
    nodes = rpc.call('getClusterNodes')
    address = args.tpu or nodes[0]['tpuQuic']
    payers = [tx.Keypair.generate() for _ in range(args.size)]
    for pair in payers:
        rpc.wait_landed(rpc.call('requestAirdrop', [pair.address, 100_000_000]))
    ledger = Ledger(None, 10**12)
    ledger.path = 'memory'  # free test SOL on a throwaway cluster: nothing to protect, the other checks still run
    auth = authorize(True, rpc, ledger, payers, localnet=True)
    ledger.path = None
    lock = tx.lock_address(f'burst-{time.time()}')
    bursts = []

    async def go():
        link = await Wire(auth=auth).open(address)
        rng = random.Random(args.seed)
        for b in range(args.bursts):
            blockhash = rpc.blockhash()['blockhash']
            if args.shuffle:  # different locks and random prices finish out of arrival order, which shuffles the free slab keys
                noise = [tx.build_probe(payers[i], tx.lock_address(f'noise-{b}-{i}'), blockhash,
                                        tx.Template(session.TEMPLATE.cu_limit, rng.randint(1_000_000, 9_000_000), session.TEMPLATE.loaded_limit),
                                        session.memo_label('nse', b, chr(97 + i))) for i in range(args.size)]
                for raw, _ in noise:
                    link.send(raw)
                await asyncio.sleep(0.15)
            batch = [tx.build_probe(payers[i], lock, blockhash, session.TEMPLATE, session.memo_label('bst', b, chr(97 + i)))
                     for i in range(args.size)]
            for raw, _ in batch:
                link.send(raw)
            bursts.append([signature for _, signature in batch])
            await asyncio.sleep(args.pause)
        await asyncio.sleep(2)
        await link.close()

    asyncio.run(go())
    cache = {}
    in_order = reversed_ = pairs = same_slot = landed = 0
    for signatures in bursts:
        statuses = rpc.call('getSignatureStatuses', [signatures, dict(searchTransactionHistory=True)])['value']
        places = []
        for i, status in enumerate(statuses):
            if status and not status.get('err'):
                view = results.block_view(rpc, status['slot'], cache)
                places.append((i, status['slot'], view['positions'][signatures[i]]))
        landed += len(places)
        for x in range(len(places)):
            for y in range(x + 1, len(places)):
                if places[x][1] != places[y][1]:
                    continue
                same_slot += 1
                pairs += 1
                if places[x][2] < places[y][2]:
                    in_order += 1
                else:
                    reversed_ += 1
    version = nodes[0].get('version')
    print(f'{nodes[0].get("clientId")} {version}: сплесків {len(bursts)} по {args.size}, сіло {landed}, пар в одному слоті {pairs}, '
          f'у порядку надсилання {in_order}, навпаки {reversed_}, частка {in_order / pairs if pairs else 0:.3f}')


def cmd_sweep(args):
    rpc = Rpc(args.rpc)
    work = load_key(args, 'work')
    payers = [(name, load_key(args, name)) for name in ('payer-a', 'payer-b')]
    if not args.live:
        for name, pair in payers + [('work', work)]:
            print(f'{name}: {sol(rpc.balance(pair.address))}')
        print(f'dry run: нічого не надіслано. З --live усе піде на {args.to}.')
        return
    ledger = ledger_for(args, rpc)
    auth = authorize(True, rpc, ledger, [work], args.localnet)
    balances = {name: rpc.balance(pair.address) for name, pair in payers + [('work', work)]}
    announce(args, ledger, [f'sweep ({auth.network}): усе на {args.to}'] +
             [f'  {name} {pair.address}: {sol(balances[name])}' for name, pair in payers + [('work', work)]] +
             [f'  усього {sol(sum(balances.values()))}, комісії до {sol(3 * tx.LAMPORTS_PER_SIGNATURE)}'])
    for name, pair in payers:
        balance = rpc.balance(pair.address)
        if balance > tx.LAMPORTS_PER_SIGNATURE:
            raw, signature = tx.build_transfer(pair, work.address, balance - tx.LAMPORTS_PER_SIGNATURE, rpc.blockhash()['blockhash'])
            submit(rpc, ledger, auth, raw, signature, tx.LAMPORTS_PER_SIGNATURE, 'sweep')
    balance = rpc.balance(work.address)
    if balance > tx.LAMPORTS_PER_SIGNATURE:
        raw, signature = tx.build_transfer(work, args.to, balance - tx.LAMPORTS_PER_SIGNATURE, rpc.blockhash()['blockhash'])
        submit(rpc, ledger, auth, raw, signature, tx.LAMPORTS_PER_SIGNATURE, 'sweep')


def cmd_power(args):
    from . import power
    print(power.table())


def parser():
    p = argparse.ArgumentParser(prog='tiepairs', description=__doc__)
    p.add_argument('--rpc', default=os.environ.get('TIEPAIRS_RPC', PUBLIC_RPC))
    p.add_argument('--ws', default=os.environ.get('TIEPAIRS_WS'))
    p.add_argument('--targets', default=os.environ.get('TIEPAIRS_TARGETS', os.path.join(HERE, 'targets.json')))
    p.add_argument('--hypotheses', default=os.path.join(HERE, 'hypotheses', 'H5.md'))
    p.add_argument('--out', default=os.environ.get('TIEPAIRS_OUT', os.path.join(HERE, 'out')))
    p.add_argument('--secrets', default=os.environ.get('TIEPAIRS_SECRETS', os.path.join(HERE, 'secrets')))
    p.add_argument('--block', default='T', help='budget line in the shared ledger')
    p.add_argument('--cap-sol', type=float, default=0.01, help='hard cap of this block, survives restarts')
    p.add_argument('--project-cap-sol', type=float, default=0.05, help='hard cap of the whole measurement, across blocks and runs')
    p.add_argument('--raise-cap', action='store_true', help='allow a cap different from the one in the ledger')
    p.add_argument('--localnet', action='store_true', help='a live command may target a local cluster')
    p.add_argument('--localnet-group', default='treated')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('keygen').set_defaults(fn=cmd_keygen)
    sub.add_parser('balances').set_defaults(fn=cmd_balances)
    sub.add_parser('targets').set_defaults(fn=cmd_targets)
    c = sub.add_parser('plan')
    c.add_argument('--pairs', type=int, default=30)
    c.set_defaults(fn=cmd_plan)
    c = sub.add_parser('calibrate')
    c.add_argument('--payer', default=HANDY_FUNDED)
    c.set_defaults(fn=cmd_calibrate)
    c = sub.add_parser('fund')
    c.add_argument('--each-lamports', type=int, default=4_800_000, help='270 probes each, plus rent, plus a margin')
    c.add_argument('--live', action='store_true')
    c.set_defaults(fn=cmd_fund)
    c = sub.add_parser('prereg')
    c.add_argument('--live', action='store_true')
    c.set_defaults(fn=cmd_prereg)
    c = sub.add_parser('run')
    c.add_argument('--pairs', type=int, default=30, help='pairs per target in a live run')
    c.add_argument('--windows', type=int, default=2, help='windows per target in a dry run')
    c.add_argument('--gap-ms', type=float, default=10.0)
    c.add_argument('--anchor-slots', type=int, default=session.Config.anchor_slots)
    c.add_argument('--stamp-linger', type=float, default=12.0, help='seconds to wait for late WebSocket stamps after the last pair')
    c.add_argument('--preflight-payer', default=HANDY_FUNDED, help='funded account for the dry-run simulation')
    c.add_argument('--run-id')
    c.add_argument('--resume', action='store_true', help='continue the last live run after a crash or a power cut')
    c.add_argument('--address-map', help='JSON identity -> host:port, for a localnet behind a port mapping')
    c.add_argument('--live', action='store_true', help='actually send and spend')
    c.set_defaults(fn=cmd_run)
    c = sub.add_parser('results')
    c.add_argument('run')
    c.set_defaults(fn=cmd_results)
    c = sub.add_parser('analyze')
    c.add_argument('runs', nargs='+')
    c.add_argument('--report')
    c.set_defaults(fn=cmd_analyze)
    c = sub.add_parser('verify')
    c.add_argument('run')
    c.set_defaults(fn=cmd_verify)
    c = sub.add_parser('airdrop')
    c.add_argument('--lamports', type=int, default=1_000_000_000)
    c.set_defaults(fn=cmd_airdrop)
    c = sub.add_parser('burst')
    c.add_argument('--bursts', type=int, default=30)
    c.add_argument('--size', type=int, default=12)
    c.add_argument('--pause', type=float, default=1.0)
    c.add_argument('--shuffle', action='store_true', help='churn the slab before each burst, as live traffic does')
    c.add_argument('--seed', type=int, default=11)
    c.add_argument('--tpu', help='host:port, when gossip advertises an address this host cannot reach')
    c.set_defaults(fn=cmd_burst)
    c = sub.add_parser('sweep')
    c.add_argument('--to', required=True)
    c.add_argument('--live', action='store_true')
    c.set_defaults(fn=cmd_sweep)
    sub.add_parser('power').set_defaults(fn=cmd_power)
    return p


def main():
    args = parser().parse_args()
    try:
        args.fn(args)
    except (Refused, CapExceeded, LedgerBusy, session.PreflightFailed) as e:
        sys.exit(f'відмова: {e}')
