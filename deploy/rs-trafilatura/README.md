# rs-trafilatura (pinned build input)

Upstream: https://github.com/Murrough-Foley/rs-trafilatura at commit `2a4f56eef84cca2d319d27c9108373d593110355` (v0.2.2). License: MIT OR Apache-2.0.

The `Dockerfile` builder stage fetches that commit, builds `extract_stdin` with this `Cargo.lock` (`cargo build --locked`, Rust 1.97.1) and copies only the binary to `/usr/local/bin/rs-trafilatura`. Upstream ships no lockfile, and a fresh resolve needs Rust 1.88 or newer while its `rust-toolchain.toml` pins 1.85, so the build drops that file and uses this lock. To bump: build the new commit, copy its resolved `Cargo.lock` here, change the commit in the `Dockerfile` and here, and rerun `benchmark/run_wcxb.py` (dev and test) with `ARGUS_RS_TRAFILATURA` pointing at the new binary. Runtime use: `src/argus/extract/rs.py`.
