# zstd arena

One repository, one protocol, every Rust-side Zstandard implementation that
matters right now, plus the C reference. CI is the judge: same runner, same
corpus, same frames, rotated order, raw samples kept.

## Who is in the ring

| slot | what | identity |
|---|---|---|
| `zstd-c` | the C reference, run as the `zstd` CLI | facebook/zstd **v1.5.7**, built from source in CI (`-T1`) |
| `ruzstd-release` | upstream ruzstd as published | crates.io `ruzstd = 0.9.0` |
| `ruzstd-upstream` | upstream ruzstd head | `KillingSpark/zstd-rs` @ `fe37617b53d611398303d3ffeee77a0a3a72bf31` |
| `ruzstd-fork` | the local fork's integration line | `Gong-Mi/zstd-rs` @ `b4ea9a3cedabda82364e2cd2a0dca9f9b84b62e5` |
| `zstd-rs-avarok` | new pure-Rust full codec | crates.io `zstd-rs = 0.1.0` (Avarok-Cybersecurity) |

`pins.json` is the single source of truth for these identities; nothing floats.

## Fairness rules (what makes the numbers comparable)

1. **One process per measured run.** Every contestant is a process: the C
   contestant is the `zstd` CLI, the Rust ones are thin binaries over each
   crate's public API. Nobody gets an in-process advantage.
2. **Level-matched encode.** C `-1` vs `ruzstd` `Fastest` vs `zstd-rs` `level 1`;
   the second arm is C `-3` vs `zstd-rs` `level 3`. Upstream ruzstd cannot join
   the level-3 arm: its `CompressionLevel::Default/Better/Best` are marked
   `UNIMPLEMENTED`, so its L3 row is *Fastest* and is labelled that way.
3. **Identical decode input.** Decoding is measured on frames produced by the C
   reference (level 1 and level 3), handed byte-identical to every decoder.
   Each contestant also decodes its own frames (row `self:*`) for the roundtrip
   oracle, never mixed into the C-frame comparison.
4. **Rotation.** Within each round the order of contestants is rotated, so
   drift on the runner cannot favour whoever runs first.
5. **Raw samples, not just medians.** Wall clock, child CPU (`wait4` rusage —
   only that child counts) and max RSS per run, with per-round medians kept so
   drift inside one job is visible.
6. **Bytes oracle.** Every decode output must hash back to the source corpus
   (`sha256` from the corpus manifest); every frame a contestant produces must
   be decodable by the C reference back to the source. Any failure fails the job.
7. **Pinned CPU.** The driver pins itself (and therefore its children) to one
   core, preferring the small cluster (cores 0-5) on two-cluster phones.

What this arena does **not** claim: it is not a universal ranking. It measures
each library's public API path on generated corpora, on the CI runner the job
ran on, at the levels each library actually implements.

## Layout

    pins.json                 contestant identity pins
    corpus/generate.py        deterministic corpora + manifest (sha256 per file)
    drivers/arena.py          the protocol: timing, rotation, oracles, JSON, table
    impls/c/adapter.sh        C contestant (CLI wrapper)
    impls/<name>/             one cargo crate per Rust contestant (same CLI contract)
    scripts/build_all.sh      build C and/or all Rust contestants
    .github/workflows/arena.yml   the competition (x86_64 + arm64 runners)

## Run it

Locally (any Linux/aarch64 box, ~22 MiB of generated corpora):

    bash scripts/build_all.sh all
    python3 corpus/generate.py corpus-out
    ZSTD_BIN=$PWD/third_party/bin/zstd python3 drivers/arena.py \
        --workdir work --corpus corpus-out --rounds 3 --samples 5

In CI: push to `main`, open a PR, or `workflow_dispatch` with `rounds`,
`samples` and an `impls` filter. Results land in `work/results-<arch>.json`
(artifact) plus the job summary table.

`--impls zstd-c,ruzstd-fork` runs a subset; `--only-encode` skips the decode arm.
