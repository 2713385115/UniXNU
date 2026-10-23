#!/usr/bin/env python3
"""Supply the boot progress observer so NativeUfs links with XNU_BOOT_PROGRESS=0.

NativeUfs.cpp declares

    void boot_progress_poll (void) __attribute__((weak));

and calls it from the verified read path. On Darwin a plain `weak` declaration is
not a weak *import*: clang emits an ordinary undefined reference, and the link
fails with "_boot_progress_poll referenced from ufsReadWindow<...>" unless some
object defines it. The real definition lives in kits/sheng/BootProgress.c, which
is built by build-boot-progress.py, and that script only runs when
XNU_BOOT_PROGRESS=1 -- a mode that also requires NATIVE_VIRTIO_BLOCK and
OWNER_ROOT_AUDIT, neither of which this board has yet. kits/sheng does not exist
in any published repository either.

So for BOOT_PROGRESS=0 this archive defines an observer that observes nothing:
one empty function, no state, safe to call from an interrupt with a window lock
held, which is exactly the contract NativeUfs documents for it. Progress
reporting stays off; the transport is unaffected.
"""
import json
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'build/boot-progress-stub'
OUT.mkdir(parents=True, exist_ok=True)

record = json.loads((ROOT / 'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/iokit/DEVELOPMENT/'
                     'IOPlatformExpert.cpo.json').read_text().strip().rstrip(','))

SOURCE_TEXT = r'''// SPDX-License-Identifier: BSD-2-Clause-Patent
// Link-time placeholder for the boot progress observer: XNU_BOOT_PROGRESS=0.
extern "C" {

// Passive by contract: no blocking, no allocation, no transport access. An empty
// body satisfies all of it while keeping the call site's behaviour unchanged.
__attribute__((used)) void boot_progress_poll (void)
{
}

}
'''

source = OUT / 'BootProgressStub.cpp'
source.write_text(SOURCE_TEXT)
destination = OUT / 'BootProgressStub.o'

# Reuse the kernel's own compile command so the object has the same target triple,
# deployment target and language options as the code that calls into it.
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
                '-I', str(ROOT / 'kits/VerifiedRoot/include'),
                '-I', str(ROOT / 'drivers/NativeUfs')])

result = subprocess.run(command, cwd=record['directory'], capture_output=True, text=True)
(OUT / 'compile.log').write_text(result.stdout + result.stderr)
if result.returncode:
    raise SystemExit((result.stdout + result.stderr)[-3500:])

data = destination.read_bytes()
magic, cpu, subtype, kind, count = struct.unpack_from('<5I', data, 0)
if (magic, cpu, subtype, kind) != (0xfeedfacf, 0x100000c, 0, 1):
    raise SystemExit('Stub requires ordinary ARM64 output')

# The one symbol must be defined *and* externally visible (N_SECT|N_EXT = 0x0f);
# a local definition would pass a looser test and still leave the link broken.
offset, found = 32, set()
for _ in range(count):
    cmd, length = struct.unpack_from('<II', data, offset)
    if cmd == 2:
        symoff, nsyms, stroff, _ = struct.unpack_from('<4I', data, offset + 8)
        for i in range(nsyms):
            ix, kind, _, _, _ = struct.unpack_from('<IBBHQ', data, symoff + i * 16)
            if kind & 0xff != 0x0f:
                continue
            found.add(data[stroff + ix:data.index(b'\0', stroff + ix)].decode())
    offset += length
if '_boot_progress_poll' not in found:
    raise SystemExit('Stub does not export _boot_progress_poll')

archive = OUT / 'libBootProgressStub.a'
archive.unlink(missing_ok=True)
subprocess.run(['xcrun', 'ar', 'rcs', archive, destination], check=True)
print('BootProgressStub: no-op observer linked as a placeholder (progress reporting stays off)')
