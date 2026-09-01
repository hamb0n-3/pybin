#!/usr/bin/env python3
from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
import time

PYTHON_CANDIDATES = []  # filled at runtime


DEFAULT_SRC = Path("src")
DEFAULT_DST = Path("Scripts_bin")
DEFAULT_PYBIN = Path("src/Files/pybin/pybin.py")
DEFAULT_OUTDIR = Path(".")


EXCLUDE_DIR_NAMES = {
    ".git", "__pycache__", ".venv", "venv", "env", ".tox", "node_modules",
    "build", "dist", "target", ".cargo", ".github", "docs", "doc", "examples",
    "sample", "samples", "test", "tests", ".idea", ".vscode",
}

# Additional subtrees to exclude entirely (relative to src root)
EXCLUDE_SUBTREES = {
    Path("Files/pybin"),
    Path("Files/pybin2"),
    Path("vim-plugin-AnsiEsc"),
}


def is_entry_script(path: Path) -> bool:
    if path.suffix != ".py":
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False

    lines = text.splitlines()
    if lines and lines[0].startswith("#") and "python" in lines[0].lower():
        return True

    return "__name__" in text and "__main__" in text and "==" in text


def should_skip_dir(rel_dir: Path) -> bool:
    # Skip if any part is in the excluded names
    for part in rel_dir.parts:
        if part in EXCLUDE_DIR_NAMES:
            return True
    # Skip excluded subtrees
    for subtree in EXCLUDE_SUBTREES:
        try:
            rel_dir.relative_to(subtree)
            return True
        except ValueError:
            pass
    return False


def mirror_structure(src: Path, dst: Path, verbose: bool = False) -> None:
    for root, dirs, _files in os.walk(src):
        root_p = Path(root)
        rel = root_p.relative_to(src)
        if should_skip_dir(rel):
            # prune
            dirs[:] = []
            continue
        # prune children too
        dirs[:] = [d for d in dirs if not should_skip_dir(rel / d)]
        out_dir = dst / rel
        out_dir.mkdir(parents=True, exist_ok=True)
        if verbose:
            print(f"[mirror] {out_dir}")


def find_entry_scripts(src: Path) -> list[Path]:
    entries: list[Path] = []
    for root, dirs, files in os.walk(src):
        root_p = Path(root)
        rel = root_p.relative_to(src)
        if should_skip_dir(rel):
            dirs[:] = []
            continue
        # prune children too
        dirs[:] = [d for d in dirs if not should_skip_dir(rel / d)]
        for f in files:
            if f.endswith(".py"):
                p = root_p / f
                if is_entry_script(p):
                    entries.append(p)
    return entries


def find_existing_artifact(out_dir: Path, binary_basename: str) -> Path | None:
    candidates = [
        out_dir / binary_basename,
        out_dir / f"{binary_basename}.bin",
        out_dir / f"{binary_basename}.exe",
        out_dir / f"{binary_basename}.dist",
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    return None


def run_pybin_for(entry: Path, pybin_path: Path, python_bin: str, cwd: Path, outdir: Path, verbose: bool = False) -> Path:
    outdir = outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    before = set(outdir.iterdir())
    before_mtime = {p: p.stat().st_mtime for p in before}
    start_ts = time.time()

    desired_name = entry.stem
    cmd = [python_bin, str(pybin_path), "--derive-name", "--name", desired_name, "--outdir", str(outdir), str(entry)]
    if verbose:
        print("[pybin] $", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd), check=True)

    # Discover produced artifact dynamically in outdir
    if not outdir.exists():
        raise RuntimeError("pybin did not create the requested output directory")

    after = list(outdir.iterdir())
    # Prefer items that are new or updated since start time
    fresh = [p for p in after if p not in before or p.stat().st_mtime >= start_ts - 0.5 or p.stat().st_mtime > before_mtime.get(p, 0)]

    pick = None
    if fresh:
        pick = max(fresh, key=lambda p: p.stat().st_mtime)
    else:
        # Fallback: take the newest thing in outdir
        if after:
            pick = max(after, key=lambda p: p.stat().st_mtime)

    if not pick:
        raise RuntimeError(f"pybin did not produce any artifact in '{outdir}'")

    return pick


def which_python_with_nuitka(explicit: str | None = None) -> str:
    if explicit:
        return explicit

    cands: list[str] = []
    cands.append(sys.executable)
    for name in ("python3", "/usr/bin/python3", "python"):
        p = shutil.which(name) or name
        if p not in cands:
            cands.append(p)

    for cand in cands:
        try:
            proc = subprocess.run([cand, "-m", "nuitka", "--version"],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,
                                  check=True)
            return cand
        except Exception:
            continue
    # Fallback to current interpreter even if Nuitka might be missing
    return sys.executable


def main() -> int:
    ap = argparse.ArgumentParser(description="Mirror src/ and build entry scripts with pybin.py")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC, help="Source src/ root (default: src)")
    ap.add_argument("--dst", type=Path, default=DEFAULT_DST, help="Destination mirror root (default: Scripts_bin)")
    ap.add_argument("--pybin", type=Path, default=DEFAULT_PYBIN, help="Path to pybin.py (default: src/Files/pybin/pybin.py)")
    ap.add_argument("--dry-run", action="store_true", help="Print planned actions without building/copying")
    ap.add_argument("--no-build", action="store_true", help="Do not build; only copy existing artifacts from --outdir if present")
    ap.add_argument("--verbose", action="store_true", help="Verbose logging")
    ap.add_argument("--python", dest="python_bin", default=None, help="Python interpreter to run pybin.py (must have Nuitka)")
    ap.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR, help="Directory where pybin.py should emit artifacts (default: current directory)")
    args = ap.parse_args()

    src: Path = args.src.resolve()
    dst: Path = args.dst.resolve()
    pybin_path: Path = args.pybin.resolve()
    out_dir: Path = args.outdir.resolve()
    cwd = Path.cwd().resolve()

    if not src.exists() or not src.is_dir():
        print(f"[error] Source does not exist or not a directory: {src}", file=sys.stderr)
        return 2
    if not pybin_path.exists():
        print(f"[error] pybin.py not found: {pybin_path}", file=sys.stderr)
        return 2
    if out_dir.exists() and not out_dir.is_dir():
        print(f"[error] --outdir exists but is not a directory: {out_dir}", file=sys.stderr)
        return 2

    python_bin = which_python_with_nuitka(args.python_bin)

    print(f"[info] Source:      {src}")
    print(f"[info] Destination: {dst}")
    print(f"[info] pybin:       {pybin_path}")
    print(f"[info] python:      {python_bin}")
    print(f"[info] outdir:      {out_dir}")

    # 1) Mirror directory structure
    if not args.dry_run:
        mirror_structure(src, dst, verbose=args.verbose)
    else:
        print("[dry] Would mirror structure")

    # 2) Discover entry scripts
    entries = find_entry_scripts(src)
    if not entries:
        print("[warn] No entry scripts detected.")
        return 0

    print(f"[info] Detected {len(entries)} entry script(s).")

    # 3) Build each entry (unless --no-build) and copy into mirror
    for entry in entries:
        rel_dir = entry.parent.relative_to(src)
        dst_dir = dst / rel_dir
        binary_basename = entry.stem  # name without .py

        print(f"[build] {entry} -> {dst_dir / binary_basename}")
        if args.dry_run:
            continue

        # Decide source artifact
        if args.no_build:
            produced = find_existing_artifact(out_dir, binary_basename) if out_dir.exists() else None
            if not produced:
                print(f"[warn] No existing artifact for {binary_basename} in {out_dir}; skipping")
                continue
        else:
            try:
                produced = run_pybin_for(entry, pybin_path, python_bin=python_bin, cwd=cwd, outdir=out_dir, verbose=args.verbose)
            except subprocess.CalledProcessError as e:
                print(f"[error] pybin failed for {entry}: {e}", file=sys.stderr)
                continue

        # Rename the produced artifact to avoid overwrite on subsequent builds
        renamed = produced.with_name(binary_basename + produced.suffix)
        # Avoid renaming to the same path
        if renamed != produced:
            if renamed.exists():
                if renamed.is_dir():
                    shutil.rmtree(renamed)
                else:
                    renamed.unlink()
            produced.rename(renamed)

        # Ensure target dir exists (mirror should have created it, but be safe)
        dst_dir.mkdir(parents=True, exist_ok=True)

        # Copy the binary into the mirrored directory
        final_target = dst_dir / renamed.name
        if final_target.exists():
            if final_target.is_dir():
                shutil.rmtree(final_target)
            else:
                final_target.unlink()
        if renamed.is_dir():
            shutil.copytree(renamed, final_target)
        else:
            shutil.copy2(renamed, final_target)

        if args.verbose:
            if final_target.is_file():
                try:
                    sz = final_target.stat().st_size
                    print(f"[done] {final_target} ({sz:,} bytes)")
                except Exception:
                    print(f"[done] {final_target}")
            else:
                print(f"[done] {final_target}/ (directory)")

    print("[info] All done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
