"""Legacy transactions built by hand, plus the fee, cost and priority arithmetic behind them.

Every probe has the same shape: one signature, two write locks (payer, shared lock account) and the same
instruction data length. Two members of a pair therefore tie under any ordering key the client could use.
Cost and priority follow Agave v4.3.2 and v4.4.0: cost-model/src/block_cost_limits.rs and
priority = reward * 1_000_000 / (cost + 1), where reward = priority fee + the unburned half of the signature fee.
"""
import hashlib
import json
import os
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
INDEX = {c: i for i, c in enumerate(ALPHABET)}

SYSTEM_PROGRAM = '1' * 32
COMPUTE_BUDGET = 'ComputeBudget111111111111111111111111111111'
MEMO_PROGRAM = 'MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr'

LAMPORTS_PER_SIGNATURE = 5_000
SIGNATURE_COST = 720
WRITE_LOCK_UNITS = 300
DATA_BYTES_PER_CU = 4
LOADED_PAGE_BYTES = 32 * 1024
LOADED_PAGE_COST = 8
DEFAULT_LOADED_LIMIT = 64 * 1024 * 1024
PRIORITY_MULTIPLIER = 1_000_000
PACKET_DATA_SIZE = 1232
LAMPORTS_PER_SOL = 1_000_000_000


def b58encode(data):
    n = int.from_bytes(data, 'big')
    out = ''
    while n:
        n, r = divmod(n, 58)
        out = ALPHABET[r] + out
    return '1' * (len(data) - len(data.lstrip(b'\0'))) + out


def b58decode(text):
    n = 0
    for c in text:
        n = n * 58 + INDEX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return b'\0' * (len(text) - len(text.lstrip('1'))) + body


def key_bytes(address):
    raw = b58decode(address)
    if len(raw) != 32:
        raise ValueError(f'not a 32-byte address: {address}')
    return raw


def compact_u16(n):
    out = bytearray()
    while True:
        byte = n & 0x7f
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_compact_u16(buf, at):
    value = shift = 0
    while True:
        byte = buf[at]
        at += 1
        value |= (byte & 0x7f) << shift
        shift += 7
        if not byte & 0x80:
            return value, at


class Keypair:
    def __init__(self, seed):
        self.seed = seed
        self.private = Ed25519PrivateKey.from_private_bytes(seed)
        self.public = self.private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.address = b58encode(self.public)

    @classmethod
    def generate(cls):
        return cls(os.urandom(32))

    @classmethod
    def load(cls, path):
        with open(path) as f:
            raw = bytes(json.load(f))
        if len(raw) != 64:
            raise ValueError(f'{path}: expected a 64-byte keypair')
        pair = cls(raw[:32])
        if pair.public != raw[32:]:
            raise ValueError(f'{path}: public half does not match the seed')
        return pair

    def dump(self, path):
        """The same JSON array the Solana CLI uses. Refuses to overwrite and creates the file as 0600."""
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(list(self.seed + self.public), f)

    def sign(self, message):
        return self.private.sign(message)


@dataclass(frozen=True)
class Template:
    cu_limit: int
    cu_price: int  # microlamports per CU
    loaded_limit: int = DEFAULT_LOADED_LIMIT

    def priority_fee(self):
        return (self.cu_limit * self.cu_price + 999_999) // 1_000_000

    def total_fee(self, signatures=1):
        return LAMPORTS_PER_SIGNATURE * signatures + self.priority_fee()

    def reward(self, signatures=1):
        base = LAMPORTS_PER_SIGNATURE * signatures
        return self.priority_fee() + base - base // 2

    def cost(self, data_bytes, write_locks=2, signatures=1):
        pages = (self.loaded_limit + LOADED_PAGE_BYTES - 1) // LOADED_PAGE_BYTES
        return (self.cu_limit + SIGNATURE_COST * signatures + WRITE_LOCK_UNITS * write_locks
                + data_bytes // DATA_BYTES_PER_CU + pages * LOADED_PAGE_COST)

    def priority(self, data_bytes, write_locks=2, signatures=1):
        return self.reward(signatures) * PRIORITY_MULTIPLIER // (self.cost(data_bytes, write_locks, signatures) + 1)


def budget_instructions(template):
    return [struct.pack('<BI', 2, template.cu_limit),
            struct.pack('<BQ', 3, template.cu_price),
            struct.pack('<BI', 4, template.loaded_limit)]


def transfer_data(lamports):
    return struct.pack('<IQ', 2, lamports)


def probe_message(payer, lock, blockhash, template, memo):
    """Accounts: payer, lock, then the three read-only programs. The zero transfer exists only to write-lock `lock`."""
    keys = [payer, key_bytes(lock), key_bytes(COMPUTE_BUDGET), key_bytes(MEMO_PROGRAM), key_bytes(SYSTEM_PROGRAM)]
    ixs = [(2, [], data) for data in budget_instructions(template)]
    ixs.append((4, [0, 1], transfer_data(0)))
    ixs.append((3, [], memo))
    return compile_message(1, 0, 3, keys, blockhash, ixs)


def compile_message(signers, readonly_signed, readonly_unsigned, keys, blockhash, ixs):
    out = bytearray([signers, readonly_signed, readonly_unsigned])
    out += compact_u16(len(keys)) + b''.join(keys) + key_bytes(blockhash) + compact_u16(len(ixs))
    for program, accounts, data in ixs:
        out.append(program)
        out += compact_u16(len(accounts)) + bytes(accounts) + compact_u16(len(data)) + data
    return bytes(out)


def signed(message, signers):
    sigs = [k.sign(message) for k in signers]
    raw = compact_u16(len(sigs)) + b''.join(sigs) + message
    if len(raw) > PACKET_DATA_SIZE:
        raise ValueError(f'transaction is {len(raw)} bytes')
    return raw, b58encode(sigs[0])


def probe_data_bytes(memo_len):
    return 5 + 9 + 5 + 12 + memo_len


def build_probe(payer, lock, blockhash, template, memo):
    return signed(probe_message(payer.public, lock, blockhash, template, memo), [payer])


def build_transfer(payer, to, lamports, blockhash):
    keys = [payer.public, key_bytes(to), key_bytes(SYSTEM_PROGRAM)]
    return signed(compile_message(1, 0, 1, keys, blockhash, [(2, [0, 1], transfer_data(lamports))]), [payer])


def build_memo(payer, memo, blockhash):
    keys = [payer.public, key_bytes(MEMO_PROGRAM)]
    return signed(compile_message(1, 0, 1, keys, blockhash, [(1, [], memo)]), [payer])


def lock_address(run_id):
    """A plain 32-byte address nobody holds a key for: only ever a zero-transfer recipient, so no signature is needed."""
    return b58encode(hashlib.sha256(f'tiepairs lock {run_id}'.encode()).digest())


def decode(raw):
    """Parse a serialized legacy transaction back, so the tests check the wire bytes and not the builder's intent."""
    n, at = read_compact_u16(raw, 0)
    signatures = [raw[at + 64 * i:at + 64 * (i + 1)] for i in range(n)]
    at += 64 * n
    message = raw[at:]
    header = tuple(raw[at:at + 3])
    at += 3
    count, at = read_compact_u16(raw, at)
    keys = [raw[at + 32 * i:at + 32 * (i + 1)] for i in range(count)]
    at += 32 * count
    blockhash = raw[at:at + 32]
    at += 32
    count, at = read_compact_u16(raw, at)
    instructions = []
    for _ in range(count):
        program = raw[at]
        at += 1
        length, at = read_compact_u16(raw, at)
        accounts = list(raw[at:at + length])
        at += length
        length, at = read_compact_u16(raw, at)
        instructions.append((program, accounts, raw[at:at + length]))
        at += length
    if at != len(raw):
        raise ValueError('trailing bytes')
    return dict(signatures=signatures, message=message, header=header, keys=keys, blockhash=blockhash,
                instructions=instructions)


def wire_priority(raw):
    """Fee, cost and priority as a validator would derive them from the bytes alone."""
    d = decode(raw)
    budget = key_bytes(COMPUTE_BUDGET)
    limit = price = None
    loaded = DEFAULT_LOADED_LIMIT
    data_bytes = 0
    for program, _, data in d['instructions']:
        data_bytes += len(data)
        if d['keys'][program] != budget:
            continue
        if data[0] == 2:
            limit = struct.unpack('<I', data[1:5])[0]
        elif data[0] == 3:
            price = struct.unpack('<Q', data[1:9])[0]
        elif data[0] == 4:
            loaded = struct.unpack('<I', data[1:5])[0]
    signers, readonly_signed, readonly_unsigned = d['header']
    write_locks = len(d['keys']) - readonly_signed - readonly_unsigned
    t = Template(limit, price, loaded)
    return dict(fee=t.total_fee(signers), cost=t.cost(data_bytes, write_locks, signers),
                priority=t.priority(data_bytes, write_locks, signers), write_locks=write_locks, data_bytes=data_bytes)


def verify_signature(raw):
    d = decode(raw)
    Ed25519PublicKey.from_public_bytes(d['keys'][0]).verify(d['signatures'][0], d['message'])
    return True
