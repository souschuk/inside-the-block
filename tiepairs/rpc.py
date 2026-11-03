"""JSON-RPC over plain HTTP. Backs off on 429 and never repeats sendTransaction itself: the node rebroadcasts the same bytes."""
import base64
import json
import ssl
import time
import urllib.error
import urllib.request

import certifi

MAINNET_GENESIS = '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'
PUBLIC_RPC = 'https://api.mainnet-beta.solana.com'


class RpcError(Exception):
    def __init__(self, method, error):
        super().__init__(f'{method}: {error}')
        self.method = method
        self.error = error
        self.code = error.get('code') if isinstance(error, dict) else None


class Rpc:
    def __init__(self, url=PUBLIC_RPC, timeout=30, retries=8, pause=0.0):
        self.url = url
        self.timeout = timeout
        self.retries = retries
        self.pause = pause
        self.context = ssl.create_default_context(cafile=certifi.where())
        self.calls = 0

    def call(self, method, params=None):
        body = json.dumps(dict(jsonrpc='2.0', id=1, method=method, params=params or [])).encode()
        attempts = 1 if method == 'sendTransaction' else self.retries
        delay = 1.0
        for attempt in range(attempts):
            request = urllib.request.Request(self.url, body, {'content-type': 'application/json',
                                                              'user-agent': 'tiepairs/0.1'})
            context = self.context if self.url.startswith('https') else None
            try:
                self.calls += 1
                with urllib.request.urlopen(request, timeout=self.timeout, context=context) as response:
                    reply = json.load(response)
            except urllib.error.HTTPError as e:
                if attempt + 1 < attempts and e.code in (429, 500, 502, 503, 504):
                    wait = float(e.headers.get('retry-after') or delay)
                    time.sleep(min(wait, 30))
                    delay = min(delay * 2, 30)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt + 1 < attempts:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise
            if self.pause:
                time.sleep(self.pause)
            if 'error' in reply:
                raise RpcError(method, reply['error'])
            return reply.get('result')

    def genesis(self):
        return self.call('getGenesisHash')

    def is_mainnet(self):
        return self.genesis() == MAINNET_GENESIS

    def balance(self, address):
        return self.call('getBalance', [address, dict(commitment='confirmed')])['value']

    def blockhash(self):
        return self.call('getLatestBlockhash', [dict(commitment='confirmed')])['value']

    def send(self, raw, auth, skip_preflight=False):
        from .guard import require  # guard imports this module for the genesis hash
        require(auth)
        return self.call('sendTransaction', [base64.b64encode(raw).decode(),
                                             dict(encoding='base64', skipPreflight=skip_preflight,
                                                  preflightCommitment='confirmed')])

    def simulate(self, raw, sig_verify=False):
        return self.call('simulateTransaction', [base64.b64encode(raw).decode(),
                                                 dict(encoding='base64', sigVerify=sig_verify,
                                                      replaceRecentBlockhash=not sig_verify,
                                                      commitment='confirmed')])['value']

    def wait_landed(self, signature, seconds=60):
        deadline = time.time() + seconds
        while time.time() < deadline:
            status = self.call('getSignatureStatuses', [[signature], dict(searchTransactionHistory=True)])['value'][0]
            if status and status.get('confirmationStatus') in ('confirmed', 'finalized'):
                return status
            time.sleep(1.5)
        return None


def ws_url(url):
    return url.replace('https://', 'wss://').replace('http://', 'ws://')
