#!/usr/bin/env python3
"""zstd arena driver: run every contestant under one protocol, on one machine.

Protocol (mirrors the campaign rules this arena was built for):
  * one OS process per measured run (the C contestant is the zstd CLI, so the
    comparison is process-level for everybody);
  * identical corpus for everybody, identified by the corpus manifest sha256;
  * decode is measured on *identical frames*: frames produced by the C
    reference at level 1 and level 3, handed unchanged to every decoder;
  * round-robin rotation of contestant order per round, to cancel drift;
  * wall clock, child CPU time (wait4 rusage, so only that child counts) and
    max RSS per run; raw samples are kept, never only medians;
  * bytes oracle: every decode output must hash back to the source corpus;
    every produced frame must be decodable by the C reference (and by the
    producer) back to the source corpus.

Usage:
  arena.py --workdir WORK --impls a,b,c [--rounds 3] [--samples 5] [--only-encode]
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha256_file(path, _cache={}):
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    if key in _cache:
        return _cache[key]
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    _cache[key] = digest
    return digest


def which_or_die(name):
    p = shutil.which(name)
    if not p:
        sys.exit(f"required tool not found on PATH: {name}")
    return p


def tail_err(path, n=400):
    """Last n chars of a child's captured stderr (only kept for failing runs)."""
    if not path:
        return None
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read()[-n:]
    except OSError:
        return None


def pick_cpu(allowed):
    """Prefer a small-cluster core (0-5 on the two-cluster phones this host uses)."""
    small = [c for c in sorted(allowed) if c <= 5]
    pool = small or sorted(allowed)
    return pool[len(pool) // 2]


def _spawn_wait4(argv, stderr_path=None):
    """fork+exec one process and reap it, returning (status, rusage).

    wait4 gives per-child CPU/RSS instead of the RUSAGE_CHILDREN accumulator, and
    fork/exec is used instead of os.posix_spawn because some CPython builds
    (this host's Termux python) do not expose posix_spawn.

    Child stdout goes to /dev/null (the driver's identity comes from the lock
    files, not from chatty children); stderr goes to stderr_path so a failing
    run can still be explained without flooding the job log.
    """
    pid = os.fork()
    if pid == 0:  # child
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            if stderr_path:
                errfd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                os.dup2(errfd, 2)
            os.execv(argv[0], argv)
        except OSError as exc:  # pragma: no cover
            os._exit(127)
    _pid, status, ru = os.wait4(pid, 0)
    return status, ru


def run_timed(argv, stderr_path=None, cwd=None):
    """Run one process, return wall/CPU/RSS/exit. Only this child's rusage counts."""
    t0 = time.perf_counter()
    status, ru = _spawn_wait4(argv, stderr_path)
    t1 = time.perf_counter()
    code = os.waitstatus_to_exitcode(status)
    return {
        "wall_s": t1 - t0,
        "cpu_s": ru.ru_utime + ru.ru_stime,
        "maxrss_kb": ru.ru_maxrss,
        "exit": code,
        "stderr": tail_err(stderr_path) if code != 0 else None,
    }


def run_capture(argv):
    t0 = time.perf_counter()
    status, ru = _spawn_wait4(argv)
    wall = time.perf_counter() - t0
    return {
        "wall_s": wall,
        "cpu_s": ru.ru_utime + ru.ru_stime,
        "maxrss_kb": ru.ru_maxrss,
        "exit": os.waitstatus_to_exitcode(status),
    }


# ---------------------------------------------------------------- contestants

class Contestant:
    def __init__(self, spec, workdir):
        self.spec = spec
        self.name = spec["name"]
        self.levels = spec.get("levels", {})
        self.workdir = workdir
        if spec["kind"] in ("cli", "c-driver"):
            self.bin = os.path.join(HERE, spec["bin"])
            self.prefix = []
        else:
            self.bin = os.path.join(HERE, "impls", self.name, "target", "release", spec.get("bin", self.name))
            self.prefix = []

    def encode_argv(self, inp, outp, level):
        return [self.bin, "encode", inp, outp, "--level", str(level)]

    def decode_argv(self, inp, outp):
        return [self.bin, "decode", inp, outp]

    def info(self):
        """Identity: cargo lock digest + resolved source revs, for git/dep pinned crates."""
        if self.spec["kind"] in ("cli", "c-driver"):
            return {"kind": self.spec["kind"], "bin": self.spec["bin"], "source": self.spec.get("source"),
                    "tag": self.spec.get("tag"), "api": self.spec.get("api")}
        d = os.path.join(HERE, "impls", self.name)
        out = {"kind": "rust", "dir": os.path.relpath(d, HERE)}
        for fn in ("Cargo.lock", "Cargo.toml"):
            p = os.path.join(d, fn)
            if os.path.exists(p):
                out[fn + "_sha256"] = sha256_file(p)
        lock = os.path.join(d, "Cargo.lock")
        if os.path.exists(lock):
            try:
                import re
                txt = open(lock).read()
                out["resolved"] = sorted(set(re.findall(r'(?m)^name = "([^"]+)"\nversion = "([^"]+)"', txt)))
                out["git_revs"] = sorted(set(re.findall(r'source = "git\+([^#"]+)#([0-9a-f]+)"', txt)))
            except Exception as exc:  # noqa: BLE001
                out["lock_parse_error"] = str(exc)
        return out


# ---------------------------------------------------------------- mechanics

def load_corpus(cdir):
    with open(os.path.join(cdir, "manifest.json")) as fh:
        manifest = json.load(fh)
    for name, meta in manifest["files"].items():
        meta["abs"] = os.path.join(cdir, meta["path"])
    return manifest


def produce_frames(contestants, c_contestant, manifest, workdir):
    """C reference frames at each level the C contestant is asked for; timings discarded."""
    frames = {}
    fdir = os.path.join(workdir, "frames")
    os.makedirs(fdir, exist_ok=True)
    for corpus, meta in sorted(manifest["files"].items()):
        for lkey, lvl in sorted(c_contestant.levels.items()):
            outp = os.path.join(fdir, f"{corpus}.c{lvl}.zst")
            r = run_capture(c_contestant.encode_argv(meta["abs"], outp, lvl))
            if r["exit"] != 0:
                sys.exit(f"C frame production failed: {corpus} level {lvl}")
            back = outp + ".check"
            rc = run_capture(c_contestant.decode_argv(outp, back))
            ok = rc["exit"] == 0 and sha256_file(back) == meta["sha256"]
            os.unlink(back)
            frames[f"{corpus}|c{lvl}"] = {
                "path": outp, "bytes": os.path.getsize(outp), "sha256": sha256_file(outp),
                "decodes_to_source": ok,
            }
            print(f"  frame {corpus:10s} C-{lvl:<2d} {os.path.getsize(outp):>9d} bytes  oracle={ok}")
    return frames


def median(values):
    return statistics.median(values) if values else None


def summarize(runs, key_fields):
    """Group raw runs and attach median/min/mean for wall, cpu and rss."""
    groups = {}
    for r in runs:
        groups.setdefault(tuple(r[k] for k in key_fields), []).append(r)
    out = []
    for key, rs in sorted(groups.items()):
        rec = dict(zip(key_fields, key))
        for metric in ("wall_s", "cpu_s", "maxrss_kb"):
            vals = [x[metric] for x in rs]
            rec[metric + "_median"] = median(vals)
            rec[metric + "_min"] = min(vals)
            rec[metric + "_n"] = len(vals)
            rec[metric + "_per_round"] = {
                str(rnd): median([x[metric] for x in rs if x["round"] == rnd])
                for rnd in sorted({x["round"] for x in rs})
            }
        sizes = [x["out_bytes"] for x in rs if x.get("out_bytes") is not None]
        if sizes:
            rec["out_bytes_median"] = median(sizes)
            rec["out_bytes_min"] = min(sizes)
            rec["out_bytes_max"] = max(sizes)
            rec["out_bytes_identical"] = len(set(sizes)) == 1
        rec["ok_all"] = all(x["ok"] for x in rs)
        out.append(rec)
    return out


def fmt(x, nd=3):
    return "n/a" if x is None else f"{x:.{nd}f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=os.path.join(HERE, "work"))
    ap.add_argument("--corpus", default=os.path.join(HERE, "corpus-out"))
    ap.add_argument("--impls", default="")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--out", default="")
    ap.add_argument("--only-encode", action="store_true")
    args = ap.parse_args()

    with open(os.path.join(HERE, "pins.json")) as fh:
        pins = json.load(fh)
    c_spec = dict(pins["c_reference"])
    c_spec.setdefault("kind", "c-driver")

    specs = [c_spec] + pins["contestants"]
    if args.impls:
        want = {x.strip() for x in args.impls.split(",") if x.strip()}
        specs = [s for s in specs if s["name"] in want]
    if not specs:
        sys.exit("no contestants selected")

    allowed = os.sched_getaffinity(0)
    cpu = pick_cpu(allowed)
    os.sched_setaffinity(0, {cpu})

    manifest = load_corpus(args.corpus)
    contestants = [Contestant(s, args.workdir) for s in specs]
    for c in contestants:
        if not os.path.exists(c.bin):
            sys.exit(f"contestant binary missing: {c.bin}")

    os.makedirs(args.workdir, exist_ok=True)
    print(f"[arena] pinned to cpu{cpu}; corpora={len(manifest['files'])}; contestants={[c.name for c in contestants]}")
    print("[arena] producing C reference frames")
    frames = produce_frames(contestants, contestants[0], manifest, args.workdir)
    if not all(f["decodes_to_source"] for f in frames.values()):
        sys.exit("a C reference frame failed its own bytes oracle")

    outdir = os.path.join(args.workdir, "out")
    os.makedirs(outdir, exist_ok=True)
    runs = []
    checks = []

    def add_check(kind, impl, corpus, level, ok, detail=""):
        checks.append({"kind": kind, "impl": impl, "corpus": corpus, "level": level, "ok": bool(ok), "detail": detail})

    # self frames (per contestant, untimed unless --only-encode): roundtrip oracle
    self_frames = {}
    for c in contestants:
        for corpus, meta in sorted(manifest["files"].items()):
            for lkey, lvl in sorted(c.levels.items()):
                outp = os.path.join(outdir, f"self-{c.name}-{corpus}-{lkey}.zst")
                r = run_capture(c.encode_argv(meta["abs"], outp, lvl))
                if r["exit"] != 0:
                    add_check("self_encode", c.name, corpus, lkey, False, "exit!=0")
                    continue
                back = outp + ".back"
                rd = run_capture(c.decode_argv(outp, back))
                ok = rd["exit"] == 0 and sha256_file(back) == meta["sha256"]
                add_check("self_roundtrip", c.name, corpus, lkey, ok)
                back2 = outp + ".cback"
                rc = run_capture(contestants[0].decode_argv(outp, back2))
                ok2 = rc["exit"] == 0 and sha256_file(back2) == meta["sha256"]
                add_check("c_decodes_ours", c.name, corpus, lkey, ok2)
                for p in (back, back2):
                    if os.path.exists(p):
                        os.unlink(p)
                self_frames[(c.name, corpus, lkey)] = outp
    print(f"[arena] self/cross oracle checks done: {len(checks)}")

    # ---- timed encode: level-matched (each contestant's own level map)
    enc_groups = []
    for c in contestants:
        for corpus in sorted(manifest["files"]):
            for lkey, lvl in sorted(c.levels.items()):
                enc_groups.append((corpus, c, lkey, lvl))
    for rnd in range(args.rounds):
        order = enc_groups[rnd % len(enc_groups):] + enc_groups[: rnd % len(enc_groups)] if enc_groups else []
        for corpus, c, lkey, lvl in order:
            for s in range(args.samples):
                outp = os.path.join(outdir, f"enc-{c.name}-{corpus}-{lkey}.zst")
                r = run_timed(c.encode_argv(manifest["files"][corpus]["abs"], outp, lvl),
                              stderr_path=os.path.join(args.workdir, "stderr.log"))
                runs.append({
                    "op": "encode", "impl": c.name, "corpus": corpus, "level_key": lkey,
                    "level": lvl, "round": rnd, "sample": s, "exit": r["exit"],
                    "wall_s": r["wall_s"], "cpu_s": r["cpu_s"], "maxrss_kb": r["maxrss_kb"],
                    "out_bytes": os.path.getsize(outp) if r["exit"] == 0 and os.path.exists(outp) else None,
                    "out_sha256": sha256_file(outp) if r["exit"] == 0 and os.path.exists(outp) else None,
                    "stderr": r.get("stderr"),
                    "ok": r["exit"] == 0,
                })
                # measured outputs are hashed into the record above; keeping them
                # would make job artifacts hundreds of megabytes
                try:
                    os.unlink(outp)
                except OSError:
                    pass

    # ---- timed decode: identical C frames, every decoder, plus its own frames
    dec_targets = []  # (corpus, target_key, path, allowed impls)
    for corpus in sorted(manifest["files"]):
        for lkey in sorted(contestants[0].levels):
            lvl = contestants[0].levels[lkey]
            dec_targets.append((corpus, f"c{lvl}", frames[f"{corpus}|c{lvl}"]["path"], None))
        for c in contestants:
            for lkey in sorted(c.levels):
                sp = self_frames.get((c.name, corpus, lkey))
                if sp:
                    dec_targets.append((corpus, f"self:{lkey}", sp, [c.name]))

    if not args.only_encode:
        for rnd in range(args.rounds):
            groups = []
            for corpus, tkind, tpath, allowed_impls in dec_targets:
                for c in contestants:
                    if allowed_impls is not None and c.name not in allowed_impls:
                        continue
                    groups.append((corpus, tkind, tpath, c))
            order = groups[rnd % len(groups):] + groups[: rnd % len(groups)] if groups else []
            for corpus, tkind, tpath, c in order:
                src_sha = manifest["files"][corpus]["sha256"]
                for s in range(args.samples):
                    outp = os.path.join(outdir, f"dec-{c.name}-{corpus}-{tkind.replace(':', '_')}.bin")
                    r = run_timed(c.decode_argv(tpath, outp),
                                  stderr_path=os.path.join(args.workdir, "stderr.log"))
                    ok = r["exit"] == 0 and os.path.exists(outp) and sha256_file(outp) == src_sha
                    runs.append({
                        "op": "decode", "impl": c.name, "corpus": corpus, "level_key": tkind,
                        "level": None, "round": rnd, "sample": s, "exit": r["exit"],
                        "wall_s": r["wall_s"], "cpu_s": r["cpu_s"], "maxrss_kb": r["maxrss_kb"],
                        "out_bytes": os.path.getsize(outp) if os.path.exists(outp) else None,
                        "out_sha256": sha256_file(outp) if os.path.exists(outp) else None,
                        "stderr": r.get("stderr"),
                        "ok": ok,
                    })
                    try:
                        os.unlink(outp)
                    except OSError:
                        pass

    # ---- derived table + head-to-head
    enc = summarize([r for r in runs if r["op"] == "encode"],
                    ["impl", "corpus", "level_key", "level"])
    dec = summarize([r for r in runs if r["op"] == "decode"],
                    ["impl", "corpus", "level_key"])

    head = []
    enc_by_key = {(r["impl"], r["corpus"], r["level_key"]): r for r in enc}
    for r in enc:
        if r["impl"] == "zstd-c":
            continue
        c_ref = enc_by_key.get(("zstd-c", r["corpus"], r["level_key"]))
        if not c_ref:
            continue
        head.append({
            "op": "encode", "corpus": r["corpus"], "level_key": r["level_key"], "impl": r["impl"],
            "vs": "zstd-c",
            "cpu_ratio": r["cpu_s_median"] / c_ref["cpu_s_median"] if c_ref["cpu_s_median"] else None,
            "wall_ratio": r["wall_s_median"] / c_ref["wall_s_median"] if c_ref["wall_s_median"] else None,
            "size_ratio_vs_c": (r["out_bytes_median"] / c_ref["out_bytes_median"]) if c_ref["out_bytes_median"] else None,
        })
    dec_by_key = {(r["impl"], r["corpus"], r["level_key"]): r for r in dec}
    for r in dec:
        if r["impl"] == "zstd-c":
            continue
        c_ref = dec_by_key.get(("zstd-c", r["corpus"], r["level_key"]))
        if not c_ref:
            continue
        head.append({
            "op": "decode", "corpus": r["corpus"], "level_key": r["level_key"], "impl": r["impl"],
            "vs": "zstd-c",
            "cpu_ratio": r["cpu_s_median"] / c_ref["cpu_s_median"] if c_ref["cpu_s_median"] else None,
            "wall_ratio": r["wall_s_median"] / c_ref["wall_s_median"] if c_ref["wall_s_median"] else None,
        })

    pairwise = []
    for op, rows, keyf in (("encode", enc, ("corpus", "level_key")), ("decode", dec, ("corpus", "level_key"))):
        by = {(r["impl"],) + tuple(r[k] for k in keyf): r for r in rows}
        impls = sorted({r["impl"] for r in rows})
        for i, a in enumerate(impls):
            for b in impls[i + 1:]:
                for corpus in sorted({r["corpus"] for r in rows}):
                    for lk in sorted({r["level_key"] for r in rows if r["corpus"] == corpus}):
                        ra, rb = by.get((a, corpus, lk)), by.get((b, corpus, lk))
                        if not ra or not rb:
                            continue
                        rec = {
                            "op": op, "a": a, "b": b, "corpus": corpus, "level_key": lk,
                            "cpu_ratio_a_over_b": ra["cpu_s_median"] / rb["cpu_s_median"] if rb["cpu_s_median"] else None,
                            "wall_ratio_a_over_b": ra["wall_s_median"] / rb["wall_s_median"] if rb["wall_s_median"] else None,
                        }
                        if op == "encode" and ra.get("out_bytes_median") and rb.get("out_bytes_median"):
                            rec["size_ratio_a_over_b"] = ra["out_bytes_median"] / rb["out_bytes_median"]
                        pairwise.append(rec)

    meta = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": platform.platform(),
        "machine": platform.machine(),
        "cpu_model": next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name") or l.startswith("Hardware")), "unknown"),
        "nproc_visible": os.cpu_count(),
        "affinity_allowed": sorted(allowed),
        "cpu_pinned": cpu,
        "rounds": args.rounds,
        "samples": args.samples,
        "zstd_cli": subprocess.run([shutil.which("zstd"), "--version"], capture_output=True, text=True).stdout.strip() if shutil.which("zstd") else None,
        "rustc": subprocess.run([which_or_die("rustc"), "-Vv"], capture_output=True, text=True).stdout.strip() if shutil.which("rustc") else None,
        "cargo": subprocess.run([which_or_die("cargo"), "-V"], capture_output=True, text=True).stdout.strip() if shutil.which("cargo") else None,
        "contestants": {c.name: c.spec for c in contestants},
        "contestant_identity": {c.name: c.info() for c in contestants},
    }
    result = {
        "meta": meta,
        "corpus": manifest,
        "frames": {k: {kk: vv for kk, vv in v.items() if kk != "path"} for k, v in frames.items()},
        "checks": checks,
        "runs": runs,
        "encode_summary": enc,
        "decode_summary": dec,
        "head_to_head_vs_c": head,
        "pairwise": pairwise,
    }
    outpath = args.out or os.path.join(args.workdir, "results.json")
    with open(outpath, "w") as fh:
        json.dump(result, fh, indent=1, sort_keys=True)

    # ---- printed tables
    lines = []
    lines.append(f"## zstd arena — {meta['generated_utc']} on {meta['machine']} (cpu pinned {cpu})")
    lines.append("")
    lines.append("encode (level-matched; cpu_s median / size vs C at the same level)")
    lines.append("| corpus | level | impl | cpu_s | wall_s | out bytes | vs C size | vs C cpu |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in enc:
        hh = next((h for h in head if h["op"] == "encode" and h["impl"] == r["impl"] and h["corpus"] == r["corpus"] and h["level_key"] == r["level_key"]), None)
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            r["corpus"], r["level_key"], r["impl"], fmt(r["cpu_s_median"], 4), fmt(r["wall_s_median"], 4),
            r.get("out_bytes_median", "?"),
            "n/a" if not hh or hh["size_ratio_vs_c"] is None else f"{hh['size_ratio_vs_c']*100:.2f}%",
            "n/a" if not hh or hh["cpu_ratio"] is None else f"{hh['cpu_ratio']:.2f}x"))
    lines.append("")
    lines.append("decode (identical C frames unless the row says self:*)")
    lines.append("| corpus | frames | impl | cpu_s | wall_s | oracle | vs C cpu |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in dec:
        hh = next((h for h in head if h["op"] == "decode" and h["impl"] == r["impl"] and h["corpus"] == r["corpus"] and h["level_key"] == r["level_key"]), None)
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            r["corpus"], r["level_key"], r["impl"], fmt(r["cpu_s_median"], 4), fmt(r["wall_s_median"], 4),
            "ok" if r["ok_all"] else "FAIL",
            "n/a" if not hh or hh["cpu_ratio"] is None else f"{hh['cpu_ratio']:.2f}x"))
    lines.append("")
    lines.append("pairwise (cpu_s median ratio a/b; size ratio a/b on encode)")
    lines.append("| op | corpus | level | a | b | cpu a/b | size a/b |")
    lines.append("|---|---|---|---|---|---|---|")
    for rec in sorted(pairwise, key=lambda r: (r["op"], r["corpus"], r["level_key"], r["a"], r["b"])):
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            rec["op"], rec["corpus"], rec["level_key"], rec["a"], rec["b"],
            "n/a" if rec["cpu_ratio_a_over_b"] is None else f"{rec['cpu_ratio_a_over_b']:.2f}x",
            "n/a" if not rec.get("size_ratio_a_over_b") else f"{rec['size_ratio_a_over_b']*100:.2f}%"))
    bad = [c for c in checks if not c["ok"]] + [r for r in runs if not r["ok"]]
    lines.append("")
    lines.append(f"oracle checks: {len(checks)} recorded, {len(bad)} failures; timed runs: {len(runs)}, failures: {len([r for r in runs if not r['ok']])}")
    text = "\n".join(lines)
    print(text)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as fh:
            fh.write(text + "\n")
    print(f"\n[arena] results -> {outpath}")
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
