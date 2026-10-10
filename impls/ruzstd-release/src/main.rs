//! One contestant, one process, one CLI contract:
//!   <bin> encode <in> <out> --level N
//!   <bin> decode <in> <out>
//! The driver measures the process; this binary only does the work and reports
//! what it actually did (requested vs effective level) on stdout as one JSON line.
use std::env;
use std::fs;
use std::process::exit;

use ruzstd::decoding::StreamingDecoder;
use ruzstd::encoding::{compress_to_vec, CompressionLevel};
use ruzstd::io::Read;

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
            // ruzstd's public level enum has Uncompressed and Fastest implemented;
            // Default/Better/Best are marked UNIMPLEMENTED upstream, so every
            // positive request maps to Fastest and is reported as such.
            let level = if level_req <= 0 {
                CompressionLevel::Uncompressed
            } else {
                CompressionLevel::Fastest
            };
            let out = compress_to_vec(&data[..], level);
            fs::write(outp, &out).expect("write output");
            println!(
                "{{\"impl\":\"{}\",\"op\":\"encode\",\"level_requested\":{},\"level_effective\":\"{:?}\",\"in\":{},\"out\":{}}}",
                env!("CARGO_PKG_NAME"),
                level_req,
                level,
                data.len(),
                out.len()
            );
        }
        "decode" => {
            let mut d = StreamingDecoder::new(&data[..]).expect("decoder init");
            let mut out = Vec::new();
            d.read_to_end(&mut out).expect("decode");
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
