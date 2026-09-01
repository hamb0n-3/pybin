# pybinobf — an out-of-tree LLVM obfuscation pass-plugin

A single `.so` that plugs into a **stock clang** (no OLLVM fork, no LLVM rebuild)
and obfuscates the C that Nuitka generates. `pybin.py` auto-detects and loads it.

## Passes

| Pass | Env toggle | What it does |
|------|-----------|--------------|
| String encryption | `PYBINOBF_STRINGS` | XOR-encrypts local `i8` string/byte constants; a startup constructor decrypts them in place. Defeats `strings binary`. |
| MBA substitution | `PYBINOBF_SUB` | Rewrites `+ - & | ^` as mixed boolean-arithmetic identities. |
| Bogus control flow | `PYBINOBF_BCF` | Splits blocks behind an always-true opaque predicate with a junk arm. |
| Control-flow flattening | `PYBINOBF_FLA` | OLLVM-style dispatcher: blocks hoisted under a `switch` on a state variable. |

It registers at the **OptimizerLast** extension point, so it runs *after* clang's
optimizer and the optimizer can't undo it. (This is also why `pybin.py` turns
**LTO off** when obfuscating — LTO would move optimization to link time.)

## Build

```bash
./build.sh # produces libpybinobf.so (uses llvm-config)
# or: cmake -S . -B build -G Ninja && ninja -C build # -> build/libpybinobf.so
```
Needs the matching LLVM dev headers (`sudo apt install llvm-21-dev`, to match
your clang). `pybin.py` looks for `obfuscator/libpybinobf.so` (and `build/…`).

## Use directly

```bash
clang -Oz -fpass-plugin=$PWD/libpybinobf.so file.c -o file
```

## Tuning (environment variables)

`cl::opt`/`-mllvm` flags **don't work** with `-fpass-plugin` (clang parses
`-mllvm` before it loads the plugin), so configuration is via env vars, read at
compile time. `pybin.py` forwards anything in its `OBF_ENV` dict.

| Variable | Default | Meaning |
|----------|---------|---------|
| `PYBINOBF_STRINGS` / `_SUB` / `_BCF` / `_FLA` | `1` | enable/disable each pass (`0` to disable) |
| `PYBINOBF_SUB_PROB` | `40` | % of eligible arithmetic ops to substitute (≥8-bit only) |
| `PYBINOBF_BCF_PROB` | `30` | % of eligible blocks to wrap in bogus CF |
| `PYBINOBF_FLA_MAX_BLOCKS` | `1500` | skip flattening functions larger than this (compile-time guard; `0` = no cap) |
| `PYBINOBF_STR_MAX_BYTES` | `0` | skip encrypting byte arrays larger than this (`0` = no cap). Set e.g. `65536` to leave Nuitka's multi-MB constants blob alone (avoids a big startup decrypt + `.data` bloat). |
| `PYBINOBF_SEED` | fixed | PRNG seed; fixed seed reproducible builds |

```bash
# example: flattening off, heavier substitution
PYBINOBF_FLA=0 PYBINOBF_SUB_PROB=70 clang -Oz -fpass-plugin=$PWD/libpybinobf.so f.c -o f
```

## Test

```bash
clang -O2 test_obf.c -o test_base # baseline
clang -O2 -fpass-plugin=$PWD/libpybinobf.so test_obf.c -o test_obf # obfuscated
# outputs must match (use the same argv[0]); MARKER_* must vanish from `strings`:
diff <(./test_base x) <(./test_obf x); strings test_obf | grep MARKER || echo "no leak"
```

## Honest limitations (read this)

- It only obfuscates **Nuitka's generated C** — not the CPython interpreter your
 code runs through.
- String decryption is **in place at startup**, so a memory dump of the running
 process still recovers the plaintext. This stops static `strings`, not a
 debugger.
- Flattening/BCF are **defeatable** by public symbolic-execution deobfuscators
 (angr/Miasm) and opaque-predicate solvers. This raises reverse-engineering
 cost; it is not a wall.
- Obfuscation **increases size and compile time** — directly at odds with the
 "smallest binary" goal. Dial passes back via the env vars if a real build gets
 too big or slow.
- Always **test the resulting binary** — obfuscation is invasive.
