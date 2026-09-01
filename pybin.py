#!/usr/bin/env python3
from __future__ import annotations
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

# CONFIG — EDIT AS NEEDED

APP_NAME = "myapp"
DEFAULT_OUT_DIR = Path(".")
SCRATCH_PREFIX = "pybin-"
BUILD_SUBDIR = "build"
DIST_SUBDIR = "dist"

NUITKA_MODE_ONEFILE = True
USE_CLANG_IF_AVAILABLE = True
ENABLE_LTO = True
JOBS = max(1, (os.cpu_count() or 1))

DERIVE_OUTPUT_NAME_BY_DEFAULT = True

# Python runtime flags for smaller/cleaner output
PYTHON_FLAGS = [
    "no_docstrings",
    "no_asserts",
    "isolated",
]

NOFOLLOW_IMPORT_TO = [
    "*.tests", "*.test",
    "distutils", "setuptools", "pkg_resources",
    "tkinter", "doctest",
]

ANTI_BLOAT_SWITCHES = [
    "--noinclude-setuptools-mode=nofollow",
    "--noinclude-pytest-mode=nofollow",
    "--noinclude-unittest-mode=nofollow",
]

# Packages that ship non-Python data files that Nuitka won't auto-detect.
# Each entry becomes --include-package-data=<pkg>.
INCLUDE_PACKAGE_DATA: List[str] = []

# Subdirectories inside packages that hold ctypes/native shared libraries
# (.so/.dll/.pyd/.dylib) which Nuitka's auto-detection misses — e.g. dirs with no
# __init__.py. Each native lib under (pkg, subdir) is bundled via
# --include-data-files. (NOT --include-data-dir: that SILENTLY SKIPS .so/.dll/.pyd,
# so the libraries go missing and ctypes loads like jpeglib's fail with
# "version not found".)
# Optional 3rd element: a list of filename globs — only matching libs are bundled.
# Use it to ship just the variants you need instead of every one in the dir.
INCLUDE_PACKAGE_SUBDIRS: List[Tuple] = [
    # jmipod calls jpeglib.read_dct() with no version.set(), so it uses jpeglib's
    # default "6b" only. Bundling just cjpeglib_6b drops ~40 MB of unused versions.
    # To support more libjpeg versions, add their globs (e.g. "cjpeglib_8d.*").
    ("jpeglib", "cjpeglib", ["cjpeglib_6b.*"]),
]

DO_STRIP = True
DO_UPX = False   # default off (marginal on onefile, adds startup cost); enable with --upx

UPX_BINARY = shutil.which("upx") or "upx"
UPX_FLAGS = [
    "--best",
    "--ultra-brute",
    "--lzma",
    "--no-backup",
    "-f",
    "--overlay=copy"  # keep extra data (strip removes it)
]

# Tool discovery
LLVM_STRIP = shutil.which("llvm-strip")
GNU_STRIP  = shutil.which("strip")
OBJCOPY    = (shutil.which("objcopy") or shutil.which("gobjcopy") or shutil.which("llvm-objcopy"))
PACHELF    = shutil.which("patchelf")
SSTRIP     = shutil.which("sstrip")

# Compile/link flags you requested (applied via environment)
REQ_CCFLAGS = "-Os -ffunction-sections -fdata-sections -fno-asynchronous-unwind-tables -fno-unwind-tables -fno-ident" # maybe these too? -fno-common -fno-inline -fno-omit-frame-pointer
REQ_LDFLAGS = "-Wl,--gc-sections -Wl,--as-needed -Wl,--build-id=none -s"

# Custom LLVM obfuscation pass-plugin (built from ./obfuscator). Loaded into
# clang via -fpass-plugin; Nuitka forwards CCFLAGS to the C compiler. Set an
# explicit path, or leave None to auto-detect under the script directory.
OBFUSCATOR_PLUGIN: Optional[str] = None
# Per-pass tuning via environment (the plugin reads these; -mllvm can't be used
# with -fpass-plugin). Empty = plugin defaults (all passes on). Examples:
#   {"PYBINOBF_FLA": "0"}            disable control-flow flattening
#   {"PYBINOBF_SUB_PROB": "60"}      raise MBA substitution to 60%
OBF_ENV: dict = {}

# END CONFIG


def fail(msg: str, code: int = 2) -> None:
    print(f"[build] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


# dependency name -> (check_fn, install_cmd, required)
#   check_fn: callable() -> bool
#   install_cmd: list of strings passed to subprocess
#   required: if True, abort when missing and user declines install
_DEPS: List[Tuple[str, Callable[[], bool], List[str], bool]] = [
    # (label, check_fn, install_cmd, required)
    ("nuitka (Python module)",
     lambda: shutil.which("nuitka") is not None or _py_module_exists("nuitka"),
     [sys.executable, "-m", "pip", "install", "nuitka"],
     True),
    ("patchelf",
     lambda: shutil.which("patchelf") is not None,
     ["sudo", "apt", "install", "-y", "patchelf"],
     platform.system() == "Linux"),
    ("clang",
     lambda: shutil.which("clang") is not None,
     ["sudo", "apt", "install", "-y", "clang"],
     False),
    ("upx",
     lambda: shutil.which("upx") is not None,
     ["sudo", "apt", "install", "-y", "upx"],
     False),
    ("strip / binutils",
     lambda: shutil.which("strip") is not None or shutil.which("llvm-strip") is not None,
     ["sudo", "apt", "install", "-y", "binutils"],
     False),
    ("objcopy",
     lambda: shutil.which("objcopy") is not None or shutil.which("llvm-objcopy") is not None,
     ["sudo", "apt", "install", "-y", "binutils"],
     False),
]


def _py_module_exists(name: str) -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def check_dependencies() -> None:
    missing_required: List[Tuple[str, List[str]]] = []
    missing_optional: List[Tuple[str, List[str]]] = []

    for label, check_fn, install_cmd, required in _DEPS:
        if not check_fn():
            if required:
                missing_required.append((label, install_cmd))
            else:
                missing_optional.append((label, install_cmd))

    if not missing_required and not missing_optional:
        return

    if missing_required:
        print("[deps] Missing required dependencies:")
        for label, cmd in missing_required:
            print(f"  - {label}  (install: {' '.join(cmd)})")
    if missing_optional:
        print("[deps] Missing optional dependencies (build will continue without them):")
        for label, cmd in missing_optional:
            print(f"  - {label}  (install: {' '.join(cmd)})")

    all_missing = missing_required + missing_optional
    try:
        answer = input("\n[deps] Install missing dependencies now? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer in ("", "y", "yes"):
        # De-duplicate install commands (e.g. binutils appears twice)
        seen: set = set()
        for label, cmd in all_missing:
            key = tuple(cmd)
            if key in seen:
                continue
            seen.add(key)
            print(f"[deps] Installing: {' '.join(cmd)}")
            try:
                subprocess.run(cmd, check=True)
            except subprocess.CalledProcessError as e:
                print(f"[deps] WARNING: install failed for '{label}': {e}")
            except FileNotFoundError:
                print(f"[deps] WARNING: installer not found for '{label}': {cmd[0]}")

        # Re-check required deps after install attempt
        still_missing = [label for label, check_fn, _cmd, required in _DEPS
                         if required and not check_fn()]
        if still_missing:
            fail("Required dependencies still missing after install: " + ", ".join(still_missing))
    else:
        if missing_required:
            fail("Cannot continue without required dependencies: " +
                 ", ".join(label for label, _ in missing_required))


def run(cmd: List[str], cwd: Path | None = None, env: dict | None = None, ignore_error: bool = False) -> int:
    print("[build] $", " ".join(cmd))
    try:
        subprocess.run(cmd, cwd=cwd, env=env, check=True)
        return 0
    except subprocess.CalledProcessError as e:
        if ignore_error:
            print(f"[build] (ignored failure: exit {e.returncode})")
            return e.returncode
        raise


def detect_clang() -> bool:
    if not USE_CLANG_IF_AVAILABLE:
        return False
    return shutil.which("clang") is not None or (
        platform.system() == "Windows" and shutil.which("clang-cl") is not None
    )


# ---- Hardening flag helpers (best-effort probes) ----

def _write_probe_c(tmp: Path) -> Path:
    code = r"""
#include <stdio.h>
#include <string.h>
int main(void) {
    char buf[32];
    strcpy(buf, "ok");
    puts(buf);
    return 0;
}
"""
    c_path = tmp / "probe.c"
    c_path.write_text(code, encoding="utf-8")
    return c_path


def _cc_supports_flags(cc: str, flags: Sequence[str], *, link: bool) -> bool:
    with tempfile.TemporaryDirectory(prefix="ccflag-probe-") as d:
        tmp = Path(d)
        c_path = _write_probe_c(tmp)

        if link:
            out = tmp / "probe-bin"
            cmd = [cc, str(c_path), "-o", str(out), *flags]
        else:
            out = tmp / "probe.o"
            cmd = [cc, "-c", str(c_path), "-o", str(out), *flags]

        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            return True
        except Exception:
            return False


def _pick_fortify_group(cc: str) -> List[str]:
    g3 = ["-U_FORTIFY_SOURCE", "-D_FORTIFY_SOURCE=3"]
    if _cc_supports_flags(cc, g3, link=False):
        return g3
    g2 = ["-U_FORTIFY_SOURCE", "-D_FORTIFY_SOURCE=2"]
    if _cc_supports_flags(cc, g2, link=False):
        return g2
    return []


def _build_hardening_flags(cc: str, arch: str, aggressive: bool = True) -> Tuple[List[str], List[str]]:
    ccflags: List[str] = []
    ldflags: List[str] = []

    candidate_cc_groups: List[List[str]] = [
        ["-fstack-protector-strong"],
        _pick_fortify_group(cc),
        ["-fPIE"],
        ["-ffunction-sections", "-fdata-sections"],
    ]
    candidate_ld_groups: List[List[str]] = [
        ["-pie"],
        ["-Wl,-z,relro"],
        ["-Wl,-z,now"],
        ["-Wl,-z,noexecstack"],
        ["-Wl,--as-needed"],
        ["-Wl,--gc-sections"],
    ]

    if aggressive:
        candidate_cc_groups += [
            ["-fstack-clash-protection"],
            ["-fno-strict-overflow"],
            ["-fno-delete-null-pointer-checks"],
            ["-fno-strict-aliasing"],
            ["-ftrivial-auto-var-init=zero"],
        ]
        candidate_ld_groups += [
            ["-Wl,-z,nodlopen"],
            ["-Wl,--no-copy-dt-needed-entries"],
        ]
        if arch in ("x86_64", "amd64"):
            candidate_cc_groups.append(["-fcf-protection=full"])
        elif arch in ("aarch64", "arm64"):
            candidate_cc_groups.append(["-mbranch-protection=standard"])

    for grp in candidate_cc_groups:
        grp = [f for f in grp if f]
        if grp and _cc_supports_flags(cc, grp, link=False):
            ccflags.extend(grp)

    for grp in candidate_ld_groups:
        grp = [f for f in grp if f]
        if grp and _cc_supports_flags(cc, grp, link=True):
            ldflags.extend(grp)

    return ccflags, ldflags


def output_filename_for_current_os() -> str:
    is_windows = platform.system() == "Windows"
    if is_windows:
        return f"{APP_NAME}.exe"
    base = APP_NAME
    if not base.endswith(".bin"):
        base = f"{base}.bin"
    return base


def find_obfuscator_plugin() -> Optional[Path]:
    cands: List[Path] = []
    env = os.environ.get("OBFUSCATOR_PLUGIN")
    if env:
        cands.append(Path(env))
    if OBFUSCATOR_PLUGIN:
        cands.append(Path(OBFUSCATOR_PLUGIN))
    here = Path(__file__).resolve().parent / "obfuscator"
    cands += [here / "libpybinobf.so", here / "build" / "libpybinobf.so"]
    for c in cands:
        try:
            if c.is_file():
                return c.resolve()
        except OSError:
            continue
    return None


_VALID_OBF_PASSES = ("strings", "sub", "bcf", "fla")


def _parse_obf_passes(spec: str) -> set:
    s = spec.strip().lower()
    if s in ("all", "*"):
        return set(_VALID_OBF_PASSES)
    if s in ("none", ""):
        return set()
    parts = [p.strip() for p in s.split(",") if p.strip()]
    bad = [p for p in parts if p not in _VALID_OBF_PASSES]
    if bad:
        fail(f"--obf-passes: unknown pass(es): {', '.join(bad)}. "
             f"Valid: {', '.join(_VALID_OBF_PASSES)} (or 'all' / 'none').")
    return set(parts)


def _resolve_obfuscation(mode: str) -> Tuple[bool, Optional[Path]]:
    plugin = find_obfuscator_plugin()
    clang = detect_clang()
    if mode == "off":
        return False, plugin
    if mode == "on":
        if not clang:
            fail("--obfuscate needs clang (set USE_CLANG_IF_AVAILABLE=True and install clang).")
        if not plugin:
            fail("--obfuscate set, but the plugin isn't built. Run: ./obfuscator/build.sh")
        return True, plugin
    return (bool(plugin) and clang), plugin


def build_with_nuitka(entry: Path, out_dir: Path, obf_mode: str = "auto",
                      obf_passes: Optional[set] = None) -> Path:
    entry = entry.resolve()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    arch = platform.machine().lower()
    cc_for_probe = (
        shutil.which("clang") if detect_clang()
        else shutil.which("cc") or shutil.which("gcc") or "cc"
    )
    ccflags_probe, ldflags_probe = _build_hardening_flags(cc_for_probe, arch, aggressive=True)

    with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX) as scratch:
        scratch_root = Path(scratch)
        build_dir = scratch_root / BUILD_SUBDIR
        dist_dir = scratch_root / DIST_SUBDIR
        build_dir.mkdir(parents=True, exist_ok=True)
        dist_dir.mkdir(parents=True, exist_ok=True)

        nuitka_cmd = [sys.executable, "-m", "nuitka"]

        if NUITKA_MODE_ONEFILE:
            nuitka_cmd += ["--onefile"]
        else:
            nuitka_cmd += ["--mode=standalone"]

        out_name = output_filename_for_current_os()
        nuitka_cmd += [f"--output-dir={build_dir}"]
        nuitka_cmd += [f"--output-filename={out_name}"]

        use_obf, plugin = _resolve_obfuscation(obf_mode)
        if ENABLE_LTO and not use_obf:
            nuitka_cmd += ["--lto=yes"]
        elif ENABLE_LTO and use_obf:
            print("[build] LTO disabled while obfuscating: the pass runs at "
                  "compile-time OptimizerLast; LTO would move optimization to "
                  "link time and undo the obfuscation.")
        if detect_clang():
            nuitka_cmd += ["--clang"]
        if JOBS:
            nuitka_cmd += [f"--jobs={JOBS}"]

        # *** Requested: reduce helpful runtime metadata
        nuitka_cmd += ["--deployment"]

        nuitka_cmd += ["--static-libpython=auto"]
        nuitka_cmd += ["--assume-yes-for-downloads"]
        nuitka_cmd += ["--remove-output"]

        if PYTHON_FLAGS:
            nuitka_cmd += [f"--python-flag={','.join(PYTHON_FLAGS)}"]

        for pat in NOFOLLOW_IMPORT_TO:
            nuitka_cmd += [f"--nofollow-import-to={pat}"]

        nuitka_cmd += ANTI_BLOAT_SWITCHES

        for pkg in INCLUDE_PACKAGE_DATA:
            nuitka_cmd += [f"--include-package-data={pkg}"]

        import importlib.util as _ilu
        import fnmatch as _fnmatch
        _LIB_EXTS = (".so", ".dll", ".pyd", ".dylib")
        for _entry in INCLUDE_PACKAGE_SUBDIRS:
            pkg_name, subdir = _entry[0], _entry[1]
            patterns = list(_entry[2]) if len(_entry) > 2 else None
            try:
                spec = _ilu.find_spec(pkg_name)
                locs = spec.submodule_search_locations if spec else None
                if not locs:
                    print(f"[build] WARNING: cannot locate package {pkg_name}; "
                          f"skipping {pkg_name}/{subdir}")
                    continue
                src = Path(list(locs)[0]) / subdir
                if not src.is_dir():
                    print(f"[build] WARNING: {src} not found; skipping {pkg_name}/{subdir}")
                    continue
                # --include-data-dir SKIPS .so/.dll/.pyd, so the native libs these
                # dirs exist for would be missing at runtime. Include each one by
                # name via --include-data-files, preserving its relative path.
                libs = sorted(p for p in src.rglob("*")
                              if p.is_file() and p.suffix in _LIB_EXTS)
                if patterns:
                    libs = [p for p in libs
                            if any(_fnmatch.fnmatch(p.name, pat) for pat in patterns)]
                for lib in libs:
                    rel = lib.relative_to(src).as_posix()
                    nuitka_cmd += [f"--include-data-files={lib}={pkg_name}/{subdir}/{rel}"]
                if libs:
                    sel = f" matching {patterns}" if patterns else ""
                    print(f"[build] {pkg_name}/{subdir}: bundled {len(libs)} native lib(s){sel}")
                else:
                    print(f"[build] WARNING: no native libs"
                          f"{' matching ' + str(patterns) if patterns else ''} found under {src}")
            except Exception as exc:
                print(f"[build] WARNING: could not resolve {pkg_name}/{subdir}: {exc}")

        # Custom LLVM obfuscation pass-plugin: string encryption, MBA instruction
        # substitution, bogus control flow, control-flow flattening. Works with a
        # stock clang (no OLLVM fork needed) — loaded via -fpass-plugin and run at
        # OptimizerLast so the optimizer can't undo it.
        extra_ccflags: List[str] = []
        if use_obf:
            extra_ccflags += [f"-fpass-plugin={plugin}"]
            shown = ",".join(sorted(obf_passes)) if obf_passes is not None else "all"
            print(f"[build] Obfuscation: ENABLED via {plugin} (passes: {shown})")
        elif obf_mode == "off":
            print("[build] Obfuscation: disabled (--no-obf).")
        elif not plugin:
            print("[build] Obfuscation: plugin not built; skipping "
                  "(build it with ./obfuscator/build.sh).")
        else:
            print("[build] Obfuscation: clang not in use; skipping "
                  "(needs --clang / USE_CLANG_IF_AVAILABLE).")

        # Env for compile/link steps (propagates to the C toolchain invoked by Nuitka)
        env = os.environ.copy()
        # Obfuscation tuning reaches the plugin through the compiler's environment.
        if use_obf:
            # ccache hashes neither the plugin .so nor the PYBINOBF_* env, so it can
            # serve un-obfuscated cached objects. Disable it for full coverage.
            env["CCACHE_DISABLE"] = "1"
            if obf_passes is not None:
                for _name, _var in (("strings", "PYBINOBF_STRINGS"),
                                    ("sub", "PYBINOBF_SUB"),
                                    ("bcf", "PYBINOBF_BCF"),
                                    ("fla", "PYBINOBF_FLA")):
                    env[_var] = "1" if _name in obf_passes else "0"
            for _k, _v in OBF_ENV.items():
                env[str(_k)] = str(_v)
        # Respect any user-provided flags but append our required ones last.
        def append_env(key: str, add: str):
            prev = env.get(key, "").strip()
            env[key] = (prev + " " + add).strip() if prev else add

        def _dedupe(seq: List[str]) -> List[str]:
            seen: set = set()
            out: List[str] = []
            for x in seq:
                if x not in seen:
                    seen.add(x)
                    out.append(x)
            return out

        # Smallest-on-disk: -Oz on clang (smaller than -Os), -Os on gcc. Appended
        # last so it wins over the -Os in REQ_CCFLAGS. (-O2 was removed from the
        # hardening probes so it can no longer override the size flag.)
        size_opt = "-Oz" if detect_clang() else "-Os"
        base_cc = REQ_CCFLAGS.split()
        # Dedupe the non-plugin C flags; append plugin flags after so the repeated
        # '-mllvm' tokens survive (a dedupe would wrongly collapse them).
        full_cc = _dedupe(base_cc + ccflags_probe + [size_opt]) + extra_ccflags
        base_ld = REQ_LDFLAGS.split()
        full_ld = _dedupe(base_ld + ldflags_probe)

        if ccflags_probe or ldflags_probe:
            print("[build] Hardening flags:",
                  f"CC={' '.join(ccflags_probe) or '(none)'}",
                  f"LD={' '.join(ldflags_probe) or '(none)'}")

        # Nuitka folds CFLAGS into CCFLAGS internally, so setting both passes every
        # flag twice; CXXFLAGS doesn't apply to the C that Nuitka generates. Set
        # CCFLAGS + LDFLAGS only.
        append_env("CCFLAGS", " ".join(full_cc))
        append_env("LDFLAGS", " ".join(full_ld))

        nuitka_cmd += [str(entry)]
        run(nuitka_cmd, env=env)

        # Find resulting artifact
        product_base = (build_dir / out_name)
        candidates = [product_base, product_base.with_suffix(".bin")]
        if not NUITKA_MODE_ONEFILE:
            candidates.append(build_dir / f"{out_name}.dist")

        product = next((c for c in candidates if c.exists()), None)
        if product is None and not NUITKA_MODE_ONEFILE:
            for d in build_dir.iterdir():
                if d.is_dir() and d.name.endswith('.dist'):
                    exe_path = d / out_name
                    if exe_path.exists():
                        product = d
                        break

        if product is None:
            fail(f"Expected output not found in {build_dir} (tried: " + ", ".join(str(c) for c in candidates) + ")")

        staged_path = dist_dir / product.name
        if staged_path.exists():
            if staged_path.is_dir():
                shutil.rmtree(staged_path)
            else:
                staged_path.unlink()
        shutil.move(str(product), str(staged_path))
        try:
            size_info = f" ({staged_path.stat().st_size:,} bytes)"
        except Exception:
            size_info = ""
        print(f"[build] Nuitka output: {staged_path}{size_info}")

        final_path = out_dir / staged_path.name
        if final_path.exists():
            if final_path.is_dir():
                shutil.rmtree(final_path)
            else:
                final_path.unlink()
        shutil.move(str(staged_path), str(final_path))
        return final_path


def _is_exec_file(p: Path) -> bool:
    try:
        return p.is_file() and os.access(p, os.X_OK)
    except Exception:
        return False


def _resolve_main_executable(container: Path) -> Path | None:
    if container.is_file():
        return container
    if container.is_dir():
        exe_name = output_filename_for_current_os()
        for cand in [container / exe_name, container / APP_NAME]:
            if _is_exec_file(cand):
                return cand
        avoid_exts = {".so", ".dylib", ".dll"}
        execs = [p for p in container.iterdir() if _is_exec_file(p) and p.suffix not in avoid_exts and ".so" not in p.name]
        if execs:
            try:
                return max(execs, key=lambda p: p.stat().st_size)
            except Exception:
                return execs[0]
    return None


def _is_elf(binary: Path) -> bool:
    try:
        with open(binary, "rb") as f:
            sig = f.read(4)
        return sig == b"\x7fELF"
    except Exception:
        return False


def strip_binary(target: Path) -> None:
    if not DO_STRIP:
        return
    binary = _resolve_main_executable(target)
    if not binary or not binary.is_file():
        return
    cmd = None
    if LLVM_STRIP:
        cmd = [LLVM_STRIP, "--strip-all", str(binary)]
    elif GNU_STRIP:
        cmd = [GNU_STRIP, "-s", str(binary)]
    if cmd:
        try:
            run(cmd)
            print(f"[build] Stripped: {binary} ({binary.stat().st_size:,} bytes)")
        except subprocess.CalledProcessError:
            print("[build] strip failed; continuing")


def shrink_rpath(binary: Path) -> None:
    if platform.system() != "Linux" or not _is_elf(binary) or not PACHELF:
        return
    # Only try if there is an RPATH/RUNPATH
    had_rpath = run([PACHELF, "--print-rpath", str(binary)], ignore_error=True) == 0
    if had_rpath:
        run([PACHELF, "--shrink-rpath", str(binary)], ignore_error=True)


def prune_sections(binary: Path) -> None:
    if platform.system() != "Linux" or not _is_elf(binary) or not OBJCOPY:
        return
    # Remove nonessential notes/comments to reduce easy identifiers
    run([OBJCOPY, "--remove-section", ".comment",
                 "--remove-section", ".note.gnu.build-id",
                 str(binary)], ignore_error=True)


def sstrip_binary(binary: Path) -> None:
    if platform.system() != "Linux" or not _is_elf(binary) or not SSTRIP:
        if platform.system() == "Linux" and not SSTRIP:
            print("[build] sstrip not found; skipping (install 'sstrip' from elfkickers)")
        return
    run([SSTRIP, str(binary)], ignore_error=True)


def upx_pack(target: Path) -> None:
    if not DO_UPX:
        return
    binary = _resolve_main_executable(target)
    if not binary or not binary.is_file():
        return
    if not shutil.which(UPX_BINARY):
        print("[build] UPX not found on PATH; skipping UPX compression")
        return
    # Do NOT preserve build-id; we just removed it.
    cmd = [UPX_BINARY, *UPX_FLAGS, str(binary)]
    try:
        run(cmd)
        print(f"[build] UPXed: {binary} ({binary.stat().st_size:,} bytes)")
        run([UPX_BINARY, "-t", str(binary)], ignore_error=True)
    except subprocess.CalledProcessError:
        print("[build] UPX failed; leaving binary uncompressed")


def break_upx_unpacker(target: Path) -> None:
    if not DO_UPX:
        return
    binary = _resolve_main_executable(target)
    if not binary or not binary.is_file():
        return
    data = bytearray(binary.read_bytes())
    idx = data.find(b"UPX!")
    if idx == -1:
        print("[build] No UPX magic found; binary not UPX-packed, skipping unpack-break")
        return
    data[idx:idx + 4] = b"\x00\x00\x00\x00"
    binary.write_bytes(bytes(data))
    print(f"[build] Broke UPX unpacker: cleared l_info magic at offset {idx}")
    if shutil.which(UPX_BINARY):
        rc = run([UPX_BINARY, "-t", str(binary)], ignore_error=True)
        if rc != 0:
            print("[build] Verified: `upx -t`/`-d` no longer recognises the file")
        else:
            print("[build] NOTE: upx still recognises the file on this version; "
                  "unpack-break may be only partial")
    print("[build] *** This mangle is fragile — verify the binary still RUNS. ***")


def main() -> None:
    check_dependencies()
    args = sys.argv[1:]
    name_opt: Optional[str] = None
    derive_name = DERIVE_OUTPUT_NAME_BY_DEFAULT
    target_path: Optional[str] = None
    out_dir: Path = DEFAULT_OUT_DIR
    obf_mode = "auto"            # "auto" | "on" | "off"
    obf_passes: Optional[set] = None
    upx_mode: Optional[bool] = None   # None = use DO_UPX config default

    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--name", "-n"):
            if i + 1 >= len(args):
                fail("--name requires a value")
            name_opt = args[i + 1]; i += 2; continue
        if a == "--derive-name":
            derive_name = True; i += 1; continue
        if a in ("--outdir", "-o"):
            if i + 1 >= len(args):
                fail("--outdir requires a value")
            out_dir = Path(args[i + 1]); i += 2; continue
        if a in ("--obfuscate", "--obf"):
            obf_mode = "on"; i += 1; continue
        if a in ("--no-obf", "--no-obfuscate"):
            obf_mode = "off"; i += 1; continue
        if a == "--obf-passes":
            if i + 1 >= len(args):
                fail("--obf-passes requires a value (e.g. strings,sub,bcf,fla)")
            obf_passes = _parse_obf_passes(args[i + 1]); i += 2; continue
        if a.startswith("--obf-passes="):
            obf_passes = _parse_obf_passes(a.split("=", 1)[1]); i += 1; continue
        if a == "--upx":
            upx_mode = True; i += 1; continue
        if a == "--no-upx":
            upx_mode = False; i += 1; continue
        if target_path is None:
            target_path = a
        i += 1

    if not target_path:
        fail("Provide an entry .py or a package directory (with __main__.py).\n"
             "Usage: python pybin.py [--name NAME | --derive-name] [--outdir DIR] [--upx]\n"
             "                       [--obfuscate | --no-obf] [--obf-passes=LIST] <entry_or_dir>")

    target = Path(target_path)
    if not target.exists():
        fail(f"Target not found: {target}")
    if out_dir.exists() and not out_dir.is_dir():
        fail(f"--outdir exists but is not a directory: {out_dir}")

    entry: Path
    global APP_NAME, DO_UPX
    if upx_mode is not None:
        DO_UPX = upx_mode
    if target.is_dir():
        main_py = target / "__main__.py"
        if not main_py.exists():
            fail(f"Directory input requires __main__.py: {main_py}")
        entry = main_py
        APP_NAME = name_opt or (target.name if derive_name else APP_NAME)
    else:
        entry = target
        APP_NAME = name_opt or (entry.stem if derive_name else APP_NAME)

    out = build_with_nuitka(entry, out_dir, obf_mode, obf_passes)

    # ---- Post-link pipeline ----
    if NUITKA_MODE_ONEFILE:
        # strip/objcopy/sstrip corrupt the appended onefile overlay, so they are
        # skipped (the binary is already stripped by Nuitka's default -s).
        # UPX is also skipped: it breaks the onefile bootstrap (verified — the
        # packed binary segfaults, with or without an l_info tweak) because the
        # bootstrap can't locate its overlay after UPX rewrites the ELF, and it
        # saves <3% since the payload is already compressed.
        if DO_UPX:
            print("[build] WARNING: UPX requested but SKIPPED for --onefile (it "
                  "breaks Nuitka's onefile bootstrap). Switch to standalone mode "
                  "(NUITKA_MODE_ONEFILE=False) if you need UPX.")
    else:
        # standalone: plain ELFs with no overlay — safe to strip fully.
        strip_binary(out)
        exe = _resolve_main_executable(out)
        if exe:
            shrink_rpath(exe)                 # patchelf --shrink-rpath
            prune_sections(exe)               # remove .comment / .note.gnu.build-id
            # sstrip is intentionally omitted: high corruption risk on the .so deps
        upx_pack(out)
        break_upx_unpacker(out)

    print("\n[build] Done.")
    print(f"[build] Final artifact: {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
