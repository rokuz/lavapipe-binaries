# Lavapipe Binaries

Pre-built [Lavapipe](https://docs.mesa3d.org/drivers/llvmpipe.html) (Mesa software Vulkan driver) for Linux x86_64 and Windows x86_64, in release and debug flavors.

## Usage

### CI (recommended)

Trigger the **Build Lavapipe** workflow via GitHub Actions with:
- **mesa_ref** — Mesa git ref (branch, tag, or commit hash; default: `main`)
- **vulkan_sdk_ref** — Vulkan-Headers/Vulkan-Loader git ref (default: `vulkan-sdk-1.4.341.0`)
- **llvm_ref** — LLVM git ref used for the Windows MSVC build (default: `llvmorg-19.1.7`)

The workflow builds each platform in both `release` and `debug` (matrix) and publishes a GitHub Release containing four zips:
`lavapipe-linux-x86_64-release.zip`, `lavapipe-linux-x86_64-debug.zip`,
`lavapipe-windows-x86_64-release.zip`, `lavapipe-windows-x86_64-debug.zip`.

On Windows, Mesa debug builds link to release LLVM (LLVM is built once as release, shared across both flavors). The debug Mesa is forced to use the release MSVC runtime (`-Db_vscrt=md`) so the CRT matches.

### Local build — Linux

```bash
# Prerequisites (Debian/Ubuntu)
sudo apt install meson ninja-build pkg-config cmake bison flex \
  llvm-dev clang \
  libx11-dev libxext-dev libxfixes-dev libxcb1-dev libxcb-randr0-dev \
  libxcb-shm0-dev libxshmfence-dev libxxf86vm-dev libxrandr-dev \
  libdrm-dev libwayland-dev wayland-protocols
pip3 install mako packaging pyyaml

# Build (default: Mesa main branch, release)
python3 build.py --mesa-ref main
# Or a debug build:
python3 build.py --mesa-ref main --build-type debug
```

### Local build — Windows (MSVC)

Run from a **x64 Native Tools Command Prompt for VS 2022**.

```bat
:: Prerequisites
choco install winflexbison pkgconfiglite ninja cmake git python glslang
pip install meson mako packaging pyyaml

:: Build (clones and builds LLVM from source the first time — slow)
python build.py --mesa-ref main
:: Or a debug build (reuses the same release LLVM):
python build.py --mesa-ref main --build-type debug

:: Or point to an existing LLVM dev install to skip the LLVM build:
python build.py --mesa-ref main --llvm-prefix C:\LLVM
```

## Output

Produces `lavapipe-<platform>-x86_64-<build-type>.zip` containing:
- `lib/` (Linux) or `bin/` (Windows) — shared libraries (Vulkan loader + lavapipe driver)
- `share/vulkan/icd.d/` — ICD manifest JSON with library path rewritten to be relative

To use the bundle, set `VK_ICD_FILENAMES` (or `VK_DRIVER_FILES`) to the absolute path of the ICD JSON, and ensure the shipped Vulkan loader is found at runtime (`LD_LIBRARY_PATH` on Linux, `PATH` on Windows).
