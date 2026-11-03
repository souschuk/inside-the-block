import pytest

from tiepairs import guard, tx
from tiepairs.guard import ALPENGLOW_SLOT, LiveAuthorization, Refused, authorize
from tiepairs.ledger import Ledger
from tiepairs.rpc import MAINNET_GENESIS, Rpc
from tiepairs.wire import Wire


class FakeRpc:
    def __init__(self, genesis=MAINNET_GENESIS, slot=ALPENGLOW_SLOT):
        self.g = genesis
        self.slot = slot

    def genesis(self):
        return self.g

    def call(self, method, params=None):
        assert method == 'getSlot'
        return self.slot


@pytest.fixture
def ledger(tmp_path):
    ledger = Ledger(str(tmp_path / 'l.jsonl'), 100)
    yield ledger
    ledger.close()


SIGNERS = [tx.Keypair.generate()]


def test_all_checks_passing_gives_a_permission(ledger):
    auth = authorize(True, FakeRpc(), ledger, SIGNERS)
    assert isinstance(auth, LiveAuthorization) and auth.network == 'mainnet'
    guard.require(auth)


def test_no_live_flag(ledger):
    with pytest.raises(Refused, match='--live'):
        authorize(False, FakeRpc(), ledger, SIGNERS)


def test_no_on_disk_ledger():
    with pytest.raises(Refused, match='ledger'):
        authorize(True, FakeRpc(), Ledger(None, 100), SIGNERS)


def test_cap_already_reached(ledger):
    ledger.reserve(100, 'probe', 'sig')
    with pytest.raises(Refused, match='cap'):
        authorize(True, FakeRpc(), ledger, SIGNERS)


def test_no_key(ledger):
    with pytest.raises(Refused, match='key'):
        authorize(True, FakeRpc(), ledger, [])


def test_wrong_genesis(ledger):
    with pytest.raises(Refused, match='not mainnet'):
        authorize(True, FakeRpc(genesis='devnet'), ledger, SIGNERS)


def test_below_alpenglow(ledger):
    with pytest.raises(Refused, match='Alpenglow'):
        authorize(True, FakeRpc(slot=ALPENGLOW_SLOT - 1), ledger, SIGNERS)


def test_localnet_needs_its_own_flag_and_a_local_genesis(ledger):
    auth = authorize(True, FakeRpc(genesis='local', slot=5), ledger, SIGNERS, localnet=True)
    assert auth.network == 'localnet'
    with pytest.raises(Refused, match='is mainnet'):
        authorize(True, FakeRpc(), ledger, SIGNERS, localnet=True)


def test_permission_cannot_be_forged():
    with pytest.raises(Refused):
        LiveAuthorization(object(), 'mainnet', MAINNET_GENESIS, ALPENGLOW_SLOT)


def test_rpc_send_needs_a_permission():
    rpc = Rpc('http://127.0.0.1:9')
    with pytest.raises(Refused):
        rpc.send(b'x', None)
    with pytest.raises(Refused):
        rpc.send(b'x', object())


def test_wire_without_permission_is_dry(tmp_path):
    assert Wire(directory=str(tmp_path)).live is False
