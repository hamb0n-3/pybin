# Portable build environment for pybin.
#
# Produces a Linux onefile that runs on any glibc >= 2.28 — i.e. essentially
# every mainstream distro from ~2018 on (Ubuntu 18.04+, Debian 10+, RHEL/Alma 8+,
# current Arch/Fedora). The base is manylinux_2_28 (AlmaLinux 8, glibc 2.28).
#
# The obfuscation pass-plugin is rebuilt INSIDE the image against the container's
# own LLVM, so it matches the clang Nuitka uses (Obfuscator.cpp is guarded for
# LLVM 17-21). The app itself is mounted at run time — see build-in-docker.sh.
#
# Build context is this directory (pybin/); it must contain pybin.py + obfuscator/.

FROM quay.io/pypa/manylinux_2_28_x86_64

# Toolchain: clang + LLVM headers (obfuscation plugin + Nuitka --clang), patchelf
# (required by Nuitka onefile on Linux), and binutils (strip/objcopy) come with
# the base image.
RUN dnf install -y clang llvm-devel patchelf && dnf clean all

# manylinux ships CPython under /opt/python/<tag>; use 3.13 to match the project.
ENV PYBIN_PY=/opt/python/cp313-cp313/bin/python

# Python build tooling + the app's third-party deps.
# NOTE: if your app grows new third-party imports, add them to this line.
RUN $PYBIN_PY -m pip install --no-cache-dir --upgrade pip && \
    $PYBIN_PY -m pip install --no-cache-dir \
        nuitka \
        numpy jpeglib pynacl

# Bring in pybin + the obfuscator and build the plugin against the image's LLVM.
COPY pybin.py /opt/pybin/pybin.py
COPY obfuscator /opt/pybin/obfuscator
RUN cd /opt/pybin/obfuscator && CXX=clang++ ./build.sh

# Give Nuitka a writable HOME/cache so the build works under an arbitrary --user.
ENV HOME=/tmp
ENV NUITKA_CACHE_DIR=/tmp/nuitka-cache

# App is mounted at /app, artifacts written to /out (both bind-mounted at run time).
WORKDIR /app
ENTRYPOINT ["/opt/python/cp313-cp313/bin/python", "/opt/pybin/pybin.py", "--obfuscate", "--outdir", "/out"]
