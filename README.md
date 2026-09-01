# pybin

Compiles a Python script into a single standalone binary with Nuitka. Wraps the
Nuitka invocation with the flags I usually want: onefile, clang + LTO, import
pruning to keep the binary small, and UPX metadata stripping. Optional
obfuscation and UPX compression.

## Usage

```
python pybin.py app.py                 # -> ./app
python pybin.py app.py -n tool -o dist # name it "tool", write to dist/
python pybin.py app.py --obfuscate
```

Build inside a container instead (portable glibc, clean toolchain):

```
./build-in-docker.sh app.py
```

`run_all.py` walks a source tree and builds every script it finds.

Tunables (compiler, LTO, anti-bloat lists, UPX) live in the CONFIG block at the
top of `pybin.py`.

## Requires

`nuitka` and a C compiler (clang preferred). `upx` if you enable compression.
