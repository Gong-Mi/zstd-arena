#!/usr/bin/env python3
"""Deterministic corpus generator for the zstd arena.

No network, no third-party text. Everything is derived from fixed seeds plus,
where available, files already present on the runner (cargo registry sources,
built binaries). The manifest records bytes + sha256 for every corpus file, and
the driver refuses to compare runs whose corpus manifest differs.

Usage: generate.py <outdir>
"""
import hashlib
import json
import os
import random
import sys

MIB = 1 << 20
SEED = 0x5A17D1E

WORDS = (
    "the of and to in a is that for it as was with be by on not he this are or his from at "
    "which but have an they one had word all were we when your can said there use each she how "
    "their if will up other about out many then them these so some her would make like him into "
    "time has look two more write go see number no way could people my than first water been call "
    "who oil its now find long down day did get come made may part over new sound take only little "
    "work know place year live me back give most very after thing our just name good sentence man "
    "think say great where help through much before line right too mean old any same tell boy follow "
    "came want show also around form three small set put end does another well large must big even "
    "such because turn here why ask went men read need land different home us move try kind hand "
    "picture again change off play spell air away animal house point page letter mother answer found "
    "study still learn should america world"
).split()


def _lorem(rng, target, line_numbers=True):
    """Readable, mildly-structured English-ish text (word list, fixed seed)."""
    out = []
    n = 0
    line = 0
    while n < target:
        line += 1
        words = [rng.choice(WORDS) for _ in range(rng.randint(60, 110))]
        if words:
            words[0] = words[0].capitalize()
        s = " ".join(words) + "."
        if line_numbers:
            s = f"{line:07d}  {s}"
        b = s.encode()
        out.append(b + b"\n")
        n += len(b) + 1
    return b"".join(out)[:target]


def _code_like(rng, target):
    """Synthesized source-like text; used when no real sources are on the runner."""
    out = []
    n = 0
    i = 0
    while n < target:
        i += 1
        kind = i % 5
        if kind == 0:
            s = f"fn handler_{i}(input: &[u8], out: &mut Vec<u8>) -> Result<usize, Error> {{"
        elif kind == 1:
            s = f"    let n = input.len().min({rng.randint(16, 4096)});"
        elif kind == 2:
            s = f"    for (idx, byte) in input[..n].iter().enumerate() {{"
        elif kind == 3:
            s = f"        out.push(byte.wrapping_add({rng.randint(0, 255)}));"
        else:
            s = f"    // {rng.choice(WORDS)} {rng.choice(WORDS)} {rng.choice(WORDS)} {i}"
        b = s.encode() + b"\n"
        out.append(b)
        n += len(b)
    return b"".join(out)[:target]


def _real_sources(target):
    """Concatenate .rs sources from the cargo registry (present after any build)."""
    roots = []
    for env in ("CARGO_HOME",):
        v = os.environ.get(env)
        if v:
            roots.append(os.path.join(v, "registry", "src"))
    roots.append(os.path.expanduser("~/.cargo/registry/src"))
    chunks = []
    for r in roots:
        if not os.path.isdir(r):
            continue
        for dirpath, _dirnames, filenames in os.walk(r):
            for fn in sorted(filenames):
                if fn.endswith(".rs"):
                    try:
                        with open(os.path.join(dirpath, fn), "rb") as fh:
                            chunks.append(fh.read())
                    except OSError:
                        pass
        if chunks:
            break
    if not chunks:
        return None
    blob = b"\n".join(chunks)
    while len(blob) < target:
        blob = blob + blob
    return blob[:target]


def _real_binaries(target, impl_dirs):
    chunks = []
    for d in impl_dirs:
        for sub in ("target/release", "target/debug"):
            p = os.path.join(d, sub)
            if not os.path.isdir(p):
                continue
            for fn in sorted(os.listdir(p)):
                fp = os.path.join(p, fn)
                if os.path.isfile(fp) and os.access(fp, os.X_OK) and not fn.endswith(".d"):
                    try:
                        with open(fp, "rb") as fh:
                            chunks.append(fh.read())
                    except OSError:
                        pass
    if not chunks:
        return None
    blob = b"\n".join(chunks)
    while len(blob) < target:
        blob = blob + blob
    return blob[:target]


def _rand(rng, target):
    out = bytearray()
    while len(out) < target:
        out += rng.getrandbits(64).to_bytes(8, "little")
    return bytes(out[:target])


def _json_records(rng, target):
    out = []
    n = 0
    i = 0
    keys = ("id", "ts", "user", "region", "path", "bytes", "status", "latency_ms", "tags", "message")
    while n < target:
        i += 1
        rec = {
            "id": f"{i:08d}-{rng.getrandbits(32):08x}",
            "ts": 1750000000 + i,
            "user": f"u{rng.randint(1, 5000)}",
            "region": rng.choice(("ap-east-1", "eu-central-1", "us-west-2", "sa-east-1")),
            "path": "/api/v2/" + "/".join(rng.choice(WORDS) for _ in range(rng.randint(1, 3))),
            "bytes": rng.randint(0, 1 << 20),
            "status": rng.choice((200, 200, 200, 204, 301, 404, 500)),
            "latency_ms": round(rng.random() * 250, 3),
            "tags": [rng.choice(WORDS) for _ in range(rng.randint(0, 3))],
            "message": " ".join(rng.choice(WORDS) for _ in range(rng.randint(4, 14))),
        }
        b = ("{" + ", ".join(f'"{k}": {json.dumps(rec[k])}' for k in keys) + "}\n").encode()
        out.append(b)
        n += len(b)
    return b"".join(out)[:target]


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else "corpus-out"
    os.makedirs(outdir, exist_ok=True)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    impl_dirs = [os.path.join(here, "impls", d) for d in os.listdir(os.path.join(here, "impls"))]

    rng = random.Random(SEED)
    built = {}
    built["text4m"] = _lorem(random.Random(SEED + 1), 4 * MIB)
    built["json1m"] = _json_records(random.Random(SEED + 2), 1 * MIB)

    src = _real_sources(4 * MIB)
    built["src4m"] = src if src is not None else _code_like(random.Random(SEED + 3), 4 * MIB)
    src_origin = "real .rs sources (cargo registry)" if src is not None else "synthesized code-like text"

    elf = _real_binaries(4 * MIB, impl_dirs)
    built["elf4m"] = elf if elf is not None else _lorem(random.Random(SEED + 4), 4 * MIB)
    elf_origin = "built binaries (target/release)" if elf is not None else "synthesized text fallback"

    built["rand4m"] = _rand(random.Random(SEED + 5), 4 * MIB)
    built["zeros1m"] = bytes(1 * MIB)
    built["small4k"] = _json_records(random.Random(SEED + 6), 4 * 1024)

    manifest = {
        "seed": SEED,
        "origins": {"src4m": src_origin, "elf4m": elf_origin},
        "files": {},
    }
    for name, data in built.items():
        path = os.path.join(outdir, name + ".bin")
        with open(path, "wb") as fh:
            fh.write(data)
        manifest["files"][name] = {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "path": os.path.relpath(path, outdir),
        }
    with open(os.path.join(outdir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    for name, meta in sorted(manifest["files"].items()):
        print(f"{name:10s} {meta['bytes']:>9d}  {meta['sha256'][:16]}")
    print(f"manifest -> {os.path.join(outdir, 'manifest.json')}")


if __name__ == "__main__":
    main()
