//! Avarok-Cybersecurity `zstd-rs` 0.1.0 contestant, same CLI contract as the others.
use std::env;
use std::fs;
use std::process::exit;

use zstd_rs::{CompressionConfig, Compressor, Decompressor};

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 4 {
        eprintln!("usage: {} <encode|decode> <in> <out> [--level N]", args[0]);
        exit(2);
    }
    let op = args[1].as_str();
    let (inp, outp) = (&args[2], &args[3]);
    let mut level_req: i32 = 1;
    let mut i = 4;
    while i < args.len() {
        if args[i] == "--level" && i + 1 < args.len() {
            level_req = args[i + 1].parse().unwrap_or(1);
            i += 2;
        } else {
            i += 1;
        }
    }
    let data = fs::read(inp).expect("read input");
    match op {
        "encode" => {
            let cfg = CompressionConfig { level: level_req, ..CompressionConfig::FAST };
            let mut c = Compressor::new(cfg).expect("compressor");
            let mut out = Vec::new();
            c.compress(&data, None, &mut out).expect("compress");
            fs::write(outp, &out).expect("write output");
            println!(
                "{{\"impl\":\"{}\",\"op\":\"encode\",\"level_requested\":{},\"level_effective\":{},\"in\":{},\"out\":{}}}",
                env!("CARGO_PKG_NAME"),
                level_req,
                level_req,
                data.len(),
                out.len()
            );
        }
        "decode" => {
            let mut d = Decompressor::new();
            let mut out = Vec::new();
            d.decompress(&data, None, 1usize << 31, &mut out).expect("decompress");
            fs::write(outp, &out).expect("write output");
            println!(
                "{{\"impl\":\"{}\",\"op\":\"decode\",\"in\":{},\"out\":{}}}",
                env!("CARGO_PKG_NAME"),
                data.len(),
                out.len()
            );
        }
        other => {
            eprintln!("unknown op: {other}");
            exit(2);
        }
    }
}
