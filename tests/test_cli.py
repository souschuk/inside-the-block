import json
import os

import pytest

from tiepairs import cli, tx


class FakeRpc:
    balances = {}
    sent = []

    def __init__(self, *args, **kwargs):
        pass

    def genesis(self):
        return '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'

    def balance(self, address):
        return self.balances.get(address, 0)

    def blockhash(self):
        return dict(blockhash=tx.b58encode(bytes(range(32))))

    def call(self, method, params=None):
        assert method == 'getSlot'
        return 460_000_000

    def send(self, raw, auth):
        FakeRpc.sent.append(tx.decode(raw))

    def wait_landed(self, signature):
        return dict(err=None)


@pytest.fixture
def keys(tmp_path, monkeypatch):
    secrets = tmp_path / 'secrets'
    secrets.mkdir()
    pairs = {}
    for name in ('work', 'payer-a', 'payer-b'):
        pairs[name] = tx.Keypair.generate()
        pairs[name].dump(str(secrets / f'{name}.json'))
    FakeRpc.sent = []
    FakeRpc.balances = {pairs['work'].address: 50_000_000}
    monkeypatch.setattr(cli, 'Rpc', FakeRpc)
    monkeypatch.setattr(cli.time, 'sleep', lambda _: None)
    return tmp_path, pairs


def run(tmp_path, *argv):
    args = cli.parser().parse_args(['--secrets', str(tmp_path / 'secrets'), '--out', str(tmp_path / 'out'), *argv])
    spec = cli.BLOCKS[args.block]
    args.hypotheses = args.hypotheses or os.path.join(cli.HERE, 'hypotheses', spec['hypotheses'])
    args.targets = args.targets or os.path.join(cli.HERE, spec['targets'])
    args.cap_sol = spec['cap_sol'] if args.cap_sol is None else args.cap_sol
    args.fn(args)
    return args


def test_a_funded_payer_is_skipped(keys):
    tmp_path, pairs = keys
    FakeRpc.balances[pairs['payer-a'].address] = 4_800_000
    run(tmp_path, 'fund', '--each-lamports', '4800000', '--live')
    assert len(FakeRpc.sent) == 1
    assert FakeRpc.sent[0]['keys'][1] == pairs['payer-b'].public


def test_fund_tops_up_to_the_target(keys):
    tmp_path, pairs = keys
    FakeRpc.balances[pairs['payer-a'].address] = 3_000_000
    FakeRpc.balances[pairs['payer-b'].address] = 4_000_000
    run(tmp_path, 'fund', '--each-lamports', '4800000', '--live')
    amounts = [int.from_bytes(s['instructions'][0][2][4:], 'little') for s in FakeRpc.sent]
    assert amounts == [1_800_000, 800_000]


def test_fund_with_everyone_funded_sends_nothing(keys):
    tmp_path, pairs = keys
    FakeRpc.balances[pairs['payer-a'].address] = FakeRpc.balances[pairs['payer-b'].address] = 9_000_000
    run(tmp_path, 'fund', '--live')
    assert FakeRpc.sent == []


def test_block_t_keeps_its_file_names_and_other_blocks_get_their_own(keys):
    tmp_path, _ = keys
    t = cli.parser().parse_args(['--out', 'o', 'plan'])
    p = cli.parser().parse_args(['--out', 'o', '--block', 'P', 'plan'])
    assert cli.block_path(t, FakeRpc(), 'prereg', 'json') == 'o/prereg-5eykt4Us.json'
    assert cli.block_path(p, FakeRpc(), 'prereg', 'json') == 'o/prereg-5eykt4Us-P.json'
    assert cli.block_path(p, FakeRpc(), 'current', 'txt') == 'o/current-5eykt4Us-P.txt'


def test_a_block_cannot_run_on_another_blocks_freeze(keys):
    tmp_path, _ = keys
    out = tmp_path / 'out'
    out.mkdir()
    (out / 'prereg-5eykt4Us.json').write_text(json.dumps(dict(sha256='x', code='y', targets='z')))
    assert not os.path.exists(cli.block_path(cli.parser().parse_args(['--out', str(out), '--block', 'P', 'plan']), FakeRpc(), 'prereg', 'json'))


def test_block_p_tops_up_to_its_own_amount(keys):
    tmp_path, pairs = keys
    FakeRpc.balances[pairs['payer-a'].address] = FakeRpc.balances[pairs['payer-b'].address] = 5_900_000
    run(tmp_path, '--block', 'P', 'fund', '--live')
    assert [int.from_bytes(s['instructions'][0][2][4:], 'little') for s in FakeRpc.sent] == [6_400_000, 6_400_000]


def memo_of(tmp_path, *argv, capsys):
    run(tmp_path, *argv, 'prereg')
    return capsys.readouterr().out


def test_the_memo_names_its_own_hypotheses(keys, capsys):
    tmp_path, _ = keys
    assert 'tiepairs H5 sha256:' in memo_of(tmp_path, capsys=capsys)
    assert 'tiepairs H6 sha256:' in memo_of(tmp_path, '--block', 'P', capsys=capsys)


def write_meta(out, name, **fields):
    (out / name).mkdir(parents=True)
    (out / name / 'meta.json').write_text(json.dumps(fields))


def test_started_sessions_are_read_from_the_runs(keys):
    tmp_path, _ = keys
    out = tmp_path / 'out'
    write_meta(out, 'r1', block='P', live=True, session=1)
    write_meta(out, 'r2', block='P', live=False, session=2)
    write_meta(out, 'r3', block='T', live=True)
    args = cli.parser().parse_args(['--out', str(out), '--block', 'P', 'plan'])
    assert cli.live_sessions(args) == {1: 'r1'}


def test_resuming_without_live_cannot_touch_a_live_run(keys):
    tmp_path, _ = keys
    with pytest.raises(SystemExit, match='add --live'):
        run(tmp_path, '--block', 'P', 'run', '--resume')


def test_a_session_outside_the_design_is_refused():
    with pytest.raises(SystemExit):
        cli.parser().parse_args(['--block', 'P', 'run', '--session', '4'])

