#!/usr/bin/env python3
"""Supply native_ufs_store_* so NativeRamBlock links with XNU_NATIVE_UFS=0.

NativeRamBlock.cpp declares the persistent-overlay store entry points

    unsigned native_ufs_store_mode (void) __attribute__((weak_import));
    int      native_ufs_store_open/read/write/sync/gpt_check (...) __attribute__((weak_import));

and calls them from attachStore(). On Darwin a plain `weak_import` declaration is
not a weak *import*: clang still emits an ordinary undefined reference, so the
link fails with

    "_native_ufs_store_read", referenced from NativeRamBlock::attachStore()

unless some object defines them. The real definitions live in the NativeUfs
transport, which this board does not build yet.

The runtime call site is already guarded (`&sym ? sym() : 0`), so this archive
only has to give the linker something to bind: mode() returns 0, which makes
attachStore() skip the overlay and keep serving the seed straight from RAM. The
block device and everything above it are unaffected; only the page write-back
between boots is off, which is what a first-light run wants anyway.
"""
import json
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'build/storage-store-stub'
OUT.mkdir(parents=True, exist_ok=True)

recipe = ROOT / 'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/iokit/DEVELOPMENT/IOPlatformExpert.cpo.json'
if not recipe.is_file():
    raise SystemExit(f'{recipe} missing: run a kernel pass first so the compile recipe exists')
record = json.loads(recipe.read_text().strip().rstrip(','))

SOURCE_TEXT = r'''// SPDX-License-Identifier: BSD-2-Clause-Patent
// Link-time placeholders for NativeRamBlock's persistent overlay (XNU_NATIVE_UFS=0).
// mode() == 0 makes attachStore() skip the overlay; the seed stays the only backing
// store, so nothing here is ever called. Returning failure rather than pretending to
// succeed keeps a future caller that ignores mode() from silently losing writes.
#include <stdint.h>

extern "C" {

__attribute__((used)) unsigned
native_ufs_store_mode (void)
{
  return 0;
}

__attribute__((used)) int
native_ufs_store_open (uint64_t, uint64_t *, const char **)
{
  return -1;
}

__attribute__((used)) int
native_ufs_store_read (uint64_t, uint32_t, void *)
{
  return -1;
}

__attribute__((used)) int
native_ufs_store_write (uint64_t, uint32_t, const void *)
{
  return -1;
}

__attribute__((used)) int
native_ufs_store_sync (void)
{
  return -1;
}

__attribute__((used)) int
native_ufs_store_gpt_check (void)
{
  return 0;
}

}
'''

source = OUT / 'StorageStoreStub.cpp'
source.write_text(SOURCE_TEXT)
destination = OUT / 'StorageStoreStub.o'

# Reuse the kernel's own compile command so the object has the same target triple,
# deployment target and language options as the object that calls into it.
arguments = record['arguments']
command = []
index = 0
while index < len(arguments):
    value = arguments[index]
    if value == record['file']:
        command.append(str(source))
    elif value == '-o':
        command.extend(['-o', str(destination)])
        index += 1
    elif value == '-Xclang' and arguments[index + 1].startswith('-W'):
        index += 1
    elif not value.startswith('-W'):
        command.append(value)
    index += 1

command.extend(['-Wall', '-Wextra', '-Werror',
                '-Wno-unused-parameter', '-I', str(ROOT / '.cache/xnu-work/osfmk')])

result = subprocess.run(command, cwd=record['directory'], capture_output=True, text=True)
(OUT / 'compile.log').write_text(result.stdout + result.stderr)
if result.returncode:
    sys.stderr.write((result.stdout + result.stderr)[-3500:])
    raise SystemExit('StorageStoreStub did not compile')

data = destination.read_bytes()
magic, cpu, subtype, kind, count = struct.unpack_from('<5I', data, 0)
if (magic, cpu, subtype, kind) != (0xfeedfacf, 0x100000c, 0, 1):
    raise SystemExit('Stub requires ordinary ARM64 output')

# Every symbol must be defined *and* externally visible (N_SECT|N_EXT = 0x0f);
# a local definition would pass a looser test and still leave the link broken.
offset, found = 32, set()
for _ in range(count):
    cmd, length = struct.unpack_from('<II', data, offset)
    if cmd == 2:                                          # LC_SYMTAB
        symoff, nsyms, stroff, _ = struct.unpack_from('<4I', data, offset + 8)
        for i in range(nsyms):
            ix, sym_kind, _, _, _ = struct.unpack_from('<IBBHQ', data, symoff + i * 16)
            if sym_kind & 0xff != 0x0f:
                continue
            found.add(data[stroff + ix:data.index(b'\0', stroff + ix)].decode())
    offset += length

required = {
    '_native_ufs_store_mode',
    '_native_ufs_store_open',
    '_native_ufs_store_read',
    '_native_ufs_store_write',
    '_native_ufs_store_sync',
    '_native_ufs_store_gpt_check',
}
missing = required - found
if missing:
    raise SystemExit(f'Stub does not export {sorted(missing)}')

archive = OUT / 'libStorageStoreStub.a'
archive.unlink(missing_ok=True)
subprocess.run(['xcrun', 'ar', 'rcs', archive, destination], check=True)
print('StorageStoreStub: no-op UFS store linked as a placeholder (persistent overlay stays off)')
