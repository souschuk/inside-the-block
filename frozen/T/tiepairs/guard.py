"""Permission to spend. Nothing can send a transaction without a LiveAuthorization, and authorize() is the only way to get one."""
from .rpc import MAINNET_GENESIS

ALPENGLOW_SLOT = 459_653_008
_SEAL = object()


class Refused(Exception):
    pass


class LiveAuthorization:
    def __init__(self, seal, network, genesis, slot):
        if seal is not _SEAL:
            raise Refused('a live authorization comes only from authorize()')
        self.network = network
        self.genesis = genesis
        self.slot = slot


def authorize(live, rpc, ledger, signers, localnet=False):
    """Checks --live, the ledger and its cap, the keys, the genesis hash and the Alpenglow threshold, in that order."""
    if not live:
        raise Refused('--live was not given')
    if ledger is None or ledger.path is None:
        raise Refused('a live command needs an on-disk ledger')
    if ledger.left() <= 0:
        raise Refused(f'nothing left under the cap: {ledger.spent} lamports spent')
    if not signers:
        raise Refused('no signing key loaded')
    genesis = rpc.genesis()
    slot = rpc.call('getSlot', [dict(commitment='confirmed')])
    if localnet:
        if genesis == MAINNET_GENESIS:
            raise Refused('--localnet was given, but this RPC is mainnet')
        return LiveAuthorization(_SEAL, 'localnet', genesis, slot)
    if genesis != MAINNET_GENESIS:
        raise Refused(f'genesis {genesis} is not mainnet: a local cluster needs --localnet')
    if slot < ALPENGLOW_SLOT:
        raise Refused(f'slot {slot} is below the Alpenglow threshold {ALPENGLOW_SLOT}')
    return LiveAuthorization(_SEAL, 'mainnet', genesis, slot)


def require(auth):
    if not isinstance(auth, LiveAuthorization):
        raise Refused('no live authorization')
