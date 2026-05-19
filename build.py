#!/usr/bin/env python3
"""Build Lavapipe (Mesa swrast Vulkan driver) and package as a zip.

Supports Linux x86_64 and Windows x86_64 (MSVC), in either release or debug
flavor. Clones Mesa, builds Vulkan-Headers and Vulkan-Loader from source,
then builds Mesa with the lavapipe (swrast) Vulkan driver and packages the
install tree as a zip.

Usage:
  python3 build.py --mesa-ref main
  python3 build.py --mesa-ref main --build-type debug
  python3 build.py --mesa-ref vulkan-sdk-1.4.341.1 --vulkan-sdk-ref vulkan-sdk-1.4.341.0
  python3 build.py --llvm-prefix C:\\LLVM   # Windows: use prebuilt LLVM

Layout (relative to cwd):
  src/                              source clones + per-type build dirs
  install/deps/                     shared (LLVM + Vulkan-Headers)
  install/<build-type>/             per-build-type install prefix (final files)
  <output-dir>/lavapipe-<plat>-<arch>-<type>.zip

Prerequisites:
  Linux (Debian/Ubuntu):
    sudo apt install ninja-build pkg-config cmake bison flex \\
      llvm-dev clang glslang-tools \\
      libx11-dev libx11-xcb-dev libxext-dev libxfixes-dev \\
      libxcb1-dev libxcb-randr0-dev libxcb-shm0-dev libxcb-keysyms1-dev \\
      libxshmfence-dev libxxf86vm-dev libxrandr-dev \\
      libdrm-dev libwayland-dev wayland-protocols
    # Mesa needs meson >= 1.4; Ubuntu 24.04's apt meson is older, use pip.
    pip3 install 'meson>=1.4.0' mako packaging pyyaml

  Windows (MSVC):
    - Visual Studio 2022 with the "Desktop development with C++" workload
    - Python 3.10+, plus: pip install meson ninja mako packaging pyyaml
    - choco install winflexbison pkgconfiglite cmake git
    - glslang is built from source by this script (no chocolatey package).
    - LLVM dev libraries: pass --llvm-prefix to a prebuilt install, or omit
      and this script will clone and build LLVM from source (~30-60 min).
    Run inside a "x64 Native Tools Command Prompt for VS 2022".
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

MESA_REPO = "https://gitlab.freedesktop.org/mesa/mesa.git"
VULKAN_HEADERS_URL = "https://github.com/KhronosGroup/Vulkan-Headers.git"
VULKAN_LOADER_URL = "https://github.com/KhronosGroup/Vulkan-Loader.git"
GLSLANG_URL = "https://github.com/KhronosGroup/glslang.git"
LLVM_REPO_URL = "https://github.com/llvm/llvm-project.git"
LLVM_DEFAULT_REF = "llvmorg-19.1.7"

BUILD_TYPES = ("release", "debug")


def run(args, **kwargs):
    print(f"  > {' '.join(str(a) for a in args)}", flush=True)
    subprocess.run([str(a) for a in args], check=True, **kwargs)


def cpu_count():
    return os.cpu_count() or 4


def platform_tag():
    if IS_WINDOWS:
        return "windows-x86_64"
    if IS_LINUX:
        return "linux-x86_64"
    raise RuntimeError(f"Unsupported platform: {platform.system()}")


def path_sep():
    return ";" if IS_WINDOWS else ":"


def check_prerequisites():
    required = ["cmake", "meson", "ninja", "git"]
    if IS_LINUX:
        required += ["pkg-config", "llvm-config"]
    missing = [c for c in required if not shutil.which(c)]
    if missing:
        print(f"ERROR: missing tools in PATH: {', '.join(missing)}")
        return False

    for pkg in ("mako", "packaging", "yaml"):
        rc = subprocess.run(
            [sys.executable, "-c", f"import {pkg}"],
            capture_output=True,
        )
        if rc.returncode != 0:
            print(f"ERROR: Python package '{pkg}' not installed")
            print("  pip install mako packaging pyyaml")
            return False

    if IS_WINDOWS:
        if not (shutil.which("win_flex") or shutil.which("flex")):
            print("ERROR: flex not found (choco install winflexbison)")
            return False
        if not (shutil.which("win_bison") or shutil.which("bison")):
            print("ERROR: bison not found (choco install winflexbison)")
            return False

    return True


def clone(repo, ref, dest, desc):
    if dest.is_dir():
        print(f"--- {desc} already present at {dest} ---")
        return dest
    print(f"--- Cloning {desc} from '{repo}' at ref '{ref}' ---")
    try:
        run(["git", "clone", "--depth", "1", "--branch", ref, repo, str(dest)])
    except subprocess.CalledProcessError:
        print("  Shallow clone failed (likely a commit hash); doing full clone...")
        if dest.is_dir():
            shutil.rmtree(dest)
        run(["git", "clone", repo, str(dest)])
        run(["git", "-C", str(dest), "checkout", ref])
    return dest


def install_vulkan_headers(src_dir, deps_dir, vulkan_sdk_ref):
    headers_src = src_dir / "vulkan-headers-src"
    headers_build = src_dir / "vulkan-headers-build"
    headers_install = deps_dir / "vulkan-headers"

    if (headers_install / "share" / "cmake" / "VulkanHeaders").is_dir():
        return headers_install

    clone(VULKAN_HEADERS_URL, vulkan_sdk_ref, headers_src, "Vulkan-Headers")
    print("--- Installing Vulkan-Headers ---")
    run([
        "cmake", "-S", str(headers_src), "-B", str(headers_build),
        f"-DCMAKE_INSTALL_PREFIX={headers_install}",
    ])
    run(["cmake", "--install", str(headers_build)])
    return headers_install


def build_and_install_vulkan_loader(src_dir, install_dir, deps_dir, vulkan_sdk_ref):
    """Build Vulkan-Loader once (Release) and install to the given install_dir.

    The loader is type-agnostic — Mesa drivers don't link to it (they're loaded
    at runtime), so a single Release build is reused across release and debug
    installs via `cmake --install --prefix`.
    """
    loader_src = src_dir / "vulkan-loader-src"
    loader_build = src_dir / "vulkan-loader-build"

    headers_prefix = install_vulkan_headers(src_dir, deps_dir, vulkan_sdk_ref)
    clone(VULKAN_LOADER_URL, vulkan_sdk_ref, loader_src, "Vulkan-Loader")

    if not (loader_build / "CMakeCache.txt").is_file():
        print("--- Configuring Vulkan-Loader ---")
        cmake_args = [
            "cmake", "-S", str(loader_src), "-B", str(loader_build),
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_PREFIX_PATH={headers_prefix}",
            "-DBUILD_TESTS=OFF",
        ]
        if IS_WINDOWS:
            cmake_args += ["-G", "Ninja"]
        run(cmake_args)
        run(["cmake", "--build", str(loader_build), "--config", "Release",
             f"-j{cpu_count()}"])

    print(f"--- Installing Vulkan-Loader -> {install_dir} ---")
    run([
        "cmake", "--install", str(loader_build),
        "--config", "Release",
        "--prefix", str(install_dir),
    ])


def build_glslang(src_dir, deps_dir, glslang_ref):
    """Build glslang (provides glslangValidator) — Windows path.

    glslang isn't packaged on Chocolatey, so we build it from source. Small
    (~2-5 min) Release build, shared across debug/release Mesa builds.
    """
    glslang_src = src_dir / "glslang-src"
    glslang_build = src_dir / "glslang-build"
    glslang_install = deps_dir / "glslang"

    if list(glslang_install.glob("bin/glslangValidator*")):
        print(f"--- glslang already installed at {glslang_install} ---")
        return glslang_install

    clone(GLSLANG_URL, glslang_ref, glslang_src, "glslang")

    # Fetch SPIRV-Tools / SPIRV-Headers into glslang's External/ tree.
    update_script = glslang_src / "update_glslang_sources.py"
    if update_script.is_file():
        print("--- Fetching glslang external sources ---")
        run([sys.executable, str(update_script)], cwd=str(glslang_src))

    print("--- Building glslang ---")
    cmake_args = [
        "cmake", "-S", str(glslang_src), "-B", str(glslang_build),
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_INSTALL_PREFIX={glslang_install}",
        "-DGLSLANG_TESTS=OFF",
        "-DENABLE_OPT=OFF",
        "-DBUILD_SHARED_LIBS=OFF",
    ]
    if IS_WINDOWS:
        cmake_args += ["-G", "Ninja"]
    run(cmake_args)
    run(["cmake", "--build", str(glslang_build), "--config", "Release",
         f"-j{cpu_count()}"])
    run(["cmake", "--install", str(glslang_build), "--config", "Release"])
    return glslang_install


def build_llvm(src_dir, deps_dir, llvm_ref):
    """Clone and build LLVM (Windows path; ~30-60 min, cache in CI).

    Always Release. Mesa debug builds on Windows pin b_vscrt=md so CRT matches.
    """
    llvm_src = src_dir / "llvm-project-src"
    llvm_build = src_dir / "llvm-project-build"
    llvm_install = deps_dir / "llvm"

    if (llvm_install / "bin").is_dir() and list(llvm_install.glob("bin/llvm-config*")):
        print(f"--- LLVM already installed at {llvm_install} ---")
        return llvm_install

    clone(LLVM_REPO_URL, llvm_ref, llvm_src, "LLVM project")
    print("--- Building LLVM (this is slow; expect 30-60 min on a CI runner) ---")
    run([
        "cmake", "-S", str(llvm_src / "llvm"), "-B", str(llvm_build),
        "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_INSTALL_PREFIX={llvm_install}",
        "-DLLVM_ENABLE_PROJECTS=",
        "-DLLVM_TARGETS_TO_BUILD=X86;AArch64",
        "-DLLVM_ENABLE_ASSERTIONS=OFF",
        "-DLLVM_ENABLE_RTTI=ON",
        "-DLLVM_ENABLE_EH=ON",
        "-DLLVM_BUILD_TOOLS=ON",
        "-DLLVM_INCLUDE_TOOLS=ON",
        "-DLLVM_BUILD_LLVM_DYLIB=OFF",
        "-DLLVM_LINK_LLVM_DYLIB=OFF",
        "-DLLVM_BUILD_TESTS=OFF",
        "-DLLVM_INCLUDE_TESTS=OFF",
        "-DLLVM_INCLUDE_EXAMPLES=OFF",
        "-DLLVM_INCLUDE_BENCHMARKS=OFF",
    ])
    run(["cmake", "--build", str(llvm_build), "--config", "Release",
         f"-j{cpu_count()}"])
    run(["cmake", "--install", str(llvm_build), "--config", "Release"])
    return llvm_install


def build_mesa(mesa_src, src_dir, install_dir, deps_dir, llvm_prefix, build_type):
    mesa_build = src_dir / f"mesa-build-{build_type}"

    print(f"--- Building Lavapipe (Mesa swrast Vulkan driver) [{build_type}] ---")

    env = os.environ.copy()
    if llvm_prefix is not None:
        env["PATH"] = f"{llvm_prefix / 'bin'}{path_sep()}{env.get('PATH', '')}"
    pkg_paths = [
        str(deps_dir / "lib" / "pkgconfig"),
        str(deps_dir / "vulkan-headers" / "share" / "pkgconfig"),
    ]
    existing_pc = env.get("PKG_CONFIG_PATH", "")
    if existing_pc:
        pkg_paths.append(existing_pc)
    env["PKG_CONFIG_PATH"] = path_sep().join(pkg_paths)

    meson_args = [
        "meson", "setup", str(mesa_src), str(mesa_build),
        f"--buildtype={build_type}",
        "-Dvulkan-drivers=swrast",
        "-Dgallium-drivers=llvmpipe",
        "-Dopengl=false",
        "-Dglx=disabled",
        "-Degl=disabled",
        "-Dgbm=disabled",
        "-Dgles1=disabled",
        "-Dgles2=disabled",
        "-Dvideo-codecs=",
        "-Dllvm=enabled",
        "-Dzstd=disabled",
        f"--prefix={install_dir}",
    ]
    if IS_WINDOWS:
        # `--prefer-static` makes find_library() demand a static-named lib
        # even for Windows SDK import libs like ws2_32, which then fails.
        # `-Dshared-llvm=disabled` already forces static LLVM linking.
        meson_args += [
            "-Dplatforms=windows",
            "-Dshared-llvm=disabled",
        ]
        if build_type == "debug":
            # Force the release CRT so Mesa links cleanly against our release LLVM
            meson_args += ["-Db_vscrt=md"]
    else:
        meson_args += ["-Dplatforms=x11,wayland"]

    if not mesa_build.is_dir():
        run(meson_args, env=env)

    run(["ninja", "-C", str(mesa_build)], env=env)
    run(["meson", "install", "-C", str(mesa_build)], env=env)


def fix_icd_library_paths(install_dir):
    """Rewrite ICD JSON library_path from absolute to a path relative to install_dir."""
    icd_dir = install_dir / "share" / "vulkan" / "icd.d"
    if not icd_dir.is_dir():
        return
    for icd_json in icd_dir.glob("*.json"):
        with open(icd_json) as f:
            data = json.load(f)
        lib_path = Path(data["ICD"]["library_path"])
        if lib_path.is_absolute():
            try:
                rel = lib_path.relative_to(install_dir)
            except ValueError:
                continue
            data["ICD"]["library_path"] = rel.as_posix()
            with open(icd_json, "w") as f:
                json.dump(data, f, indent=4, sort_keys=True, separators=(",", ": "))
                f.write("\n")
            print(f"  Fixed ICD path: {lib_path} -> {rel.as_posix()}")


def validate(install_dir):
    icd_dir = install_dir / "share" / "vulkan" / "icd.d"
    jsons = list(icd_dir.glob("*.json")) if icd_dir.is_dir() else []
    if not jsons:
        raise RuntimeError("Lavapipe ICD JSON not found after build")
    for j in jsons:
        print(f"  ICD: {j}")

    patterns = ["*.dll"] if IS_WINDOWS else ["*.so", "*.so.*"]
    search_dirs = [install_dir / "bin", install_dir / "lib"]
    found = []
    for d in search_dirs:
        if d.is_dir():
            for pat in patterns:
                found += sorted(d.glob(pat))
    if not found:
        raise RuntimeError("No driver libraries found after build")
    for lib in found:
        print(f"  Lib: {lib}")


def package(install_dir, output_dir, tag, build_type):
    zip_name = f"lavapipe-{tag}-{build_type}.zip"
    zip_path = output_dir / zip_name

    print(f"--- Packaging {zip_name} ---")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(install_dir):
            for f in files:
                full = Path(root) / f
                arcname = full.relative_to(install_dir)
                zf.write(full, arcname)
                print(f"  + {arcname}")

    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"  Output: {zip_path} ({size_mb:.1f} MB)")
    return zip_path


def main():
    parser = argparse.ArgumentParser(
        description="Build Lavapipe (Mesa swrast Vulkan driver)")
    parser.add_argument(
        "--mesa-repo", default=MESA_REPO,
        help=f"Mesa git repository URL (default: {MESA_REPO})")
    parser.add_argument(
        "--mesa-ref", default="main",
        help="Mesa git ref: branch, tag, or commit hash (default: main)")
    parser.add_argument(
        "--vulkan-sdk-ref", default="vulkan-sdk-1.4.341.0",
        help="Vulkan-Headers/Vulkan-Loader git ref (default: vulkan-sdk-1.4.341.0)")
    parser.add_argument(
        "--build-type", choices=BUILD_TYPES, default="release",
        help="Mesa build type (default: release)")
    parser.add_argument(
        "--llvm-prefix", type=Path, default=None,
        help="Path to an existing LLVM install (with llvm-config + dev libs). "
             "On Linux defaults to system llvm-config. On Windows, if omitted, "
             "LLVM is cloned and built from source.")
    parser.add_argument(
        "--llvm-ref", default=LLVM_DEFAULT_REF,
        help=f"LLVM git ref to build when --llvm-prefix is omitted on Windows "
             f"(default: {LLVM_DEFAULT_REF})")
    parser.add_argument(
        "--output-dir", type=Path, default=None,
        help="Directory for the output zip (default: current directory)")
    args = parser.parse_args()

    if not check_prerequisites():
        sys.exit(1)

    base_dir = Path.cwd()
    output_dir = args.output_dir or base_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    src_dir = base_dir / "src"
    build_root = base_dir / "install"
    deps_dir = build_root / "deps"
    install_dir = build_root / args.build_type

    for d in (src_dir, build_root, deps_dir, install_dir):
        d.mkdir(parents=True, exist_ok=True)

    llvm_prefix = args.llvm_prefix
    if llvm_prefix is None and IS_WINDOWS:
        llvm_prefix = build_llvm(src_dir, deps_dir, args.llvm_ref)
    print(f"Using LLVM prefix: {llvm_prefix or '<system llvm-config>'}")

    if IS_WINDOWS:
        # glslang isn't on chocolatey; build it and prepend to PATH so Mesa's
        # meson find_program('glslangValidator') succeeds.
        glslang_prefix = build_glslang(src_dir, deps_dir, args.vulkan_sdk_ref)
        os.environ["PATH"] = (
            f"{glslang_prefix / 'bin'}{path_sep()}{os.environ.get('PATH', '')}"
        )

    mesa_src = clone(args.mesa_repo, args.mesa_ref, src_dir / "mesa", "Mesa")

    build_and_install_vulkan_loader(src_dir, install_dir, deps_dir, args.vulkan_sdk_ref)
    build_mesa(mesa_src, src_dir, install_dir, deps_dir, llvm_prefix, args.build_type)
    fix_icd_library_paths(install_dir)
    validate(install_dir)

    zip_path = package(install_dir, output_dir, platform_tag(), args.build_type)

    print()
    print(f"--- Lavapipe {args.build_type} build complete: {zip_path} ---")


if __name__ == "__main__":
    main()
