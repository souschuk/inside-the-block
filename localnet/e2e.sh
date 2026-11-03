#!/bin/sh
# The whole path on a real validator of one version, with free test SOL: fund, prereg, pairs, results, analysis, verify, sweep.
#   docker compose --profile localnet up -d localnet-440 && localnet/e2e.sh 4.4.0
set -eu
version=${1:?usage: e2e.sh 4.3.2|4.4.0}
case "$version" in 4.3.2) base=18;; 4.4.0) base=28;; *) echo "unknown version $version"; exit 2;; esac
HERE=$(dirname "$(readlink -f "$0")")
tool=${TIEPAIRS_TOOL:-$(dirname "$HERE")/itb}
work=$(mktemp -d)
export TIEPAIRS_OUT="$work/out" TIEPAIRS_SECRETS="$work/secrets" TIEPAIRS_RPC="http://127.0.0.1:${base}899" TIEPAIRS_WS="ws://127.0.0.1:${base}900"
"$tool" --localnet keygen
"$tool" --localnet airdrop
"$tool" --localnet calibrate --payer "$("$tool" --localnet balances | awk '/^work/ {print $2}')"
"$tool" --localnet burst --bursts 10 --size 12
"$tool" --localnet fund --each-lamports 20000000 --live
"$tool" --localnet prereg --live
"$tool" --localnet run --pairs "${PAIRS:-6}" --live --stamp-linger 2
run=$(ls -td "$TIEPAIRS_OUT"/*-live-* | head -1)
"$tool" --localnet results "$run"
"$tool" --localnet analyze "$run"
"$tool" --localnet verify "$run"
"$tool" --localnet sweep --to 7y3Em9WXKa3MyRCj5Y66mhRc7kM5gCUP6B5H5XNpN352 --live   # the test validator's faucet
"$tool" --localnet balances
rm -rf "$work"
