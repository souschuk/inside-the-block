# solana-test-validator from the official Agave release tarball: there are no anzaxyz/agave images for 4.x.
FROM debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3 AS fetch
ARG AGAVE_VERSION
ARG AGAVE_SHA256
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl bzip2 && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/agave
RUN curl -fsSL --retry 5 -o release.tar.bz2 "https://github.com/anza-xyz/agave/releases/download/${AGAVE_VERSION}/solana-release-x86_64-unknown-linux-gnu.tar.bz2" \
 && echo "${AGAVE_SHA256}  release.tar.bz2" | sha256sum -c - \
 && tar -xjf release.tar.bz2 solana-release/bin/solana-test-validator solana-release/bin/solana

FROM debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libudev1 && rm -rf /var/lib/apt/lists/*
COPY --from=fetch /tmp/agave/solana-release/bin/ /usr/local/bin/
# A 0.0.0.0 bind panics in gossip, so the validator binds to the container's own address.
ENTRYPOINT ["sh", "-c", "ip=$(hostname -i | cut -d' ' -f1); exec solana-test-validator --reset --quiet --ledger /tmp/ledger --bind-address $ip --rpc-port 8899 --dynamic-port-range 8000-8030 --limit-blockstore-size 5000000 $TEST_VALIDATOR_ARGS"]
