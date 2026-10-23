#!/usr/bin/env python3
"""Build the IOStorageFamily objects the built-in block drivers link against.

Why this exists
---------------
The block drivers shipped with UniXNU (`NativeUfs`, `NativeRamBlock`,
`NativeVirtioBlock`) derive from `IOBlockStorageDevice`, but that class lives in
the IOStorageFamily kext, which is not part of XNU. The tree only keeps its
public headers, so the link ends with

    "IOBlockStorageDevice::~IOBlockStorageDevice()"
    "IOBlockStorageDevice::getProperty(OSSymbol const*) const"
    "vtable for IOBlockStorageDevice"

Apple publishes the source of that kext (refer/IOStorageFamily, pinned in
feeds.toml), so the missing definitions can be compiled straight out of it
instead of being stubbed -- a stub would satisfy the linker but publish nothing
and mountroot would still fail.

Each object is compiled with the exact command line XNU already used for its own
IOKit sources (build/xnu/ios-obj/.../IOPlatformExpert.cpo.json), with only the
input file swapped, so the ABI, the optimiser settings and the include order are
identical to the rest of the kernel. That is the same trick
scripts/build-arm-driver.py uses for the drivers themselves.

Divergence from the upstream build (deliberate)
-----------------------------------------------
* Only the sources named on the command line are built. The whole family is not
  needed to satisfy the linker, and every extra kext source is another chance to
  fail on a private header.
* -Wno-error: this is Apple kext code compiled inside a kernel build; warnings
  there are expected and are not a reason to stop.
* -fno-exceptions -fno-rtti are already in the recipe; nothing is added that
  could change the vtable layout.
"""
import argparse
import json
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REFER = ROOT / 'refer/IOStorageFamily'
OUT = ROOT / 'build/io-storage-family'
RECIPE = ROOT / 'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/iokit/DEVELOPMENT/IOPlatformExpert.cpo.json'

# The whole family, in dependency order. The default is the minimum that makes
# the block drivers link; the rest are only needed once a real IOMedia has to be
# published for mountroot.
ALL_SOURCES = [
    'IOStorage.cpp',
    'IORequest.cpp',
    'IORequestsPool.cpp',
    'IOBlockStorageDevice.cpp',
    'IOBlockStorageDriver.cpp',
    'IOFilterScheme.cpp',
    'IOPartitionScheme.cpp',
    'IOGUIDPartitionScheme.cpp',
    'IOMedia.cpp',
    'IOMediaBSDClient.cpp',
]


def fail(message):
    raise SystemExit('build-io-storage-family: ' + message)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('sources', nargs='*',
                    help='IOStorageFamily sources to compile (default: %s)' % ALL_SOURCES[3])
    args = ap.parse_args()
    sources = args.sources or ['IOBlockStorageDevice.cpp']

    if not REFER.is_dir():
        fail(f'missing {REFER}; the build fetches it with "unixnu fetch IOStorageFamily"')
    if not RECIPE.is_file():
        # Archives are built after the bootstrap pass that writes the compile
        # database; if it is missing the ordering in the build is wrong rather
        # than this script.
        fail(f'missing {RECIPE}; the compile database has not been produced yet')

    record = json.loads(RECIPE.read_text().strip().rstrip(','))
    base = record['arguments']

    OUT.mkdir(parents=True, exist_ok=True)
    objects = []
    failures = []

    for name in sources:
        source = REFER / name
        if not source.is_file():
            fail(f'missing {source}')
        destination = OUT / (source.stem + '.o')

        command = []
        index = 0
        while index < len(base):
            value = base[index]
            if value == record['file']:
                command.append(str(source))
            elif value == '-o':
                command.extend(['-o', str(destination)])
                index += 1
            elif value == '-Xclang' and index + 1 < len(base) and base[index + 1].startswith('-W'):
                index += 1
            elif not value.startswith('-W'):
                command.append(value)
            index += 1

        command.extend([
            '-Wall', '-Wextra', '-Wno-error',
            '-Wno-unused-parameter', '-Wno-unused-variable', '-Wno-unused-function',
            '-Wno-c99-designator', '-Wno-reorder-init-list',
            '-Wno-deprecated-enum-enum-conversion', '-Wno-unnecessary-virtual-specifier',
            '-Wno-invalid-offsetof', '-Wno-gnu-anonymous-struct', '-Wno-nested-anon-types',
            '-I', str(ROOT / 'build/storage-abi/include'),
            '-I', str(REFER),
        ])

        (OUT / (source.stem + '.compile-command.json')).write_text(
            json.dumps(command, indent=2) + '\n')
        result = subprocess.run(command, cwd=record['directory'],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        log = result.stdout or ''
        (OUT / (source.stem + '.compile.log')).write_text(log)
        if result.returncode:
            # Collect every file's errors instead of stopping at the first, so a
            # single CI round reports the whole picture.
            interesting = [line for line in log.splitlines()
                           if ' error: ' in line or line.startswith('error:')
                           or ' fatal error: ' in line]
            failures.append((name, interesting[:20] or log.splitlines()[-20:]))
            continue
        objects.append(destination)

    if failures:
        print('build-io-storage-family: %d of %d sources failed' % (len(failures), len(sources)))
        for name, lines in failures:
            print('--- %s' % name)
            for line in lines:
                print('    ' + line)
        sys.exit(1)

    archive = OUT / 'libIOStorageFamily.a'
    if archive.exists():
        archive.unlink()
    ar = subprocess.run(['llvm-ar', 'crs', str(archive)] + [str(o) for o in objects],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if ar.returncode:
        fail('llvm-ar failed: ' + (ar.stdout or ''))

    symbols = subprocess.run(['llvm-nm', '-gU', str(archive)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    defined = set()
    if symbols.returncode == 0:
        for line in symbols.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] in ('T', 'S', 'D', 'B', 'R', 'W', 'V'):
                defined.add(parts[2])
            elif len(parts) == 2 and parts[0] in ('T', 'S', 'D', 'B', 'R'):
                defined.add(parts[1])

    # The three symbols the block drivers were missing. Failing here is better
    # than failing 40 minutes later inside the kernel link.
    wanted = [
        '__ZN20IOBlockStorageDeviceD2Ev',
        '__ZNK20IOBlockStorageDevice11getPropertyEPK8OSSymbol',
        '__ZTV20IOBlockStorageDevice',
    ]
    missing = [w for w in wanted if w not in defined]
    report = dict(sources=sources, archive=str(archive), archive_sha256=None,
                  objects=[str(o) for o in objects], defined_symbol_count=len(defined),
                  missing_contract_symbols=missing)
    (OUT / 'build-report.json').write_text(json.dumps(report, indent=2) + '\n')

    if missing:
        print('build-io-storage-family: archive built but these symbols are absent: %s'
              % ', '.join(missing))
        print('  (demangle with llvm-cxxfilt; the mangling follows the class layout in '
              'refer/IOStorageFamily)')
        sys.exit(1)

    print('PASS: %s (%d objects, %d defined symbols)' % (archive.name, len(objects), len(defined)))


if __name__ == '__main__':
    main()
