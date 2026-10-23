#!/usr/bin/env python3
"""Link the real VerifiedRoot kit into the kernel, with owner-signed policies.

`NativeUfs` refuses to publish any root window unless each one authenticates
against an owner-signed envelope (`NativeUfsBlock::configureVerification`), so a
kernel built with `build-verified-root-stub.py` - which supplies
`verified_root_policy_count = 0` - can talk to the UFS but can never hand a block
device to APFS. This archive is the real thing:

    kits/VerifiedRoot/src/verified_root.c        vr_open, vr_verify_block
    kits/VerifiedRoot/adapters/xnu_crypto.c      SHA-256 + RSA-3072 from libkern
    verified-root-policies.S                     the two envelopes + anchor + table

Both envelopes are produced from the exact window bytes that go into the
`userdata` image (kits/VerifiedRoot/tools/manifest.py signs the ordered SHA-256 of
every 4096-byte block and the APFS volume UUID), so a read that verifies here is a
read of the original image.

Divergence from the upstream build (deliberate)
-----------------------------------------------
* The envelopes are built ahead of time, on a machine with the window images,
  instead of by `scripts/build-owner-os-policy.py`. That script and
  `scripts/build-verified-root-probe.py` gate on `build/verified-root-{os,
  systemos}/verification.json`, whose producers (the macOS host dylib and the
  SystemOS policy step) are not part of this fork, so they cannot run here. The
  envelope format and the signing are identical because both go through
  kits/VerifiedRoot/tools/manifest.py.
* The objects are compiled with the command line XNU used for its own bsd_init.o,
  so the kit sees exactly the headers and flags the rest of the kernel does.
"""
import hashlib
import json
import struct
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
OUT = ROOT / 'build/verified-root-bundle'
RECIPE = ROOT / 'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/bsd/DEVELOPMENT/bsd_init.o.json'
KIT = ROOT / 'kits/VerifiedRoot'

# Both envelopes and the anchor arrive as one directory: make-owner-policy.py writes
# it locally, gh-release-upload.py publishes it, and the CI run fetches it here
# before the kernel is linked. Order fixes the policy table order; NativeUfs picks a
# policy by matching its signed image size against the window size.
INPUTS = ROOT / 'build/verified-root-inputs'
POLICIES = [('OS', INPUTS / 'OS' / 'manifest.bin'), ('SystemOS', INPUTS / 'SystemOS' / 'manifest.bin')]
ANCHOR = INPUTS / 'trusted-key.bin'
WINDOWS = {'OS': 6077580800, 'SystemOS': 3303014400}


def fail(message):
    raise SystemExit('build-verified-root-bundle: ' + message)


def check(destination: Path) -> tuple[bytes, int]:
    """Reject an envelope that does not describe the window it claims to."""
    data = destination.read_bytes()
    if len(data) < 544 or data[0:8] != b'VROOT01\0':
        fail(f'{destination.name}: not a VROOT01 envelope')
    version, header_bytes, block_bytes, flags = struct.unpack_from('<4I', data, 8)
    image_bytes, count = struct.unpack_from('<2Q', data, 24)
    generation, = struct.unpack_from('<Q', data, 152)
    if (version, header_bytes, block_bytes, flags) != (1, 160, 4096, 0) or generation < 1:
        fail(f'{destination.name}: unexpected header {version}/{header_bytes}/{block_bytes}/{flags}/{generation}')
    if count != (image_bytes + 4095) // 4096 or len(data) != 544 + count * 32:
        fail(f'{destination.name}: {len(data)} bytes do not match {count} block hashes')
    return data, image_bytes


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if not RECIPE.is_file():
        fail(f'missing {RECIPE}; the compile database has not been produced yet')
    record = json.loads(RECIPE.read_text().strip().rstrip(','))

    anchor = ANCHOR.read_bytes()
    if len(anchor) != 395 or anchor[0:8] != b'VROOTKEY':
        fail(f'{ANCHOR.name}: expected a 395-byte VROOTKEY anchor')
    anchor_digest = hashlib.sha256(anchor).digest()

    envelopes, records = [], []
    for name, path in POLICIES:
        if not path.is_file():
            fail(f'missing {path}; the envelope has to be generated from the window image first')
        data, image_bytes = check(path)
        if WINDOWS[name] != image_bytes:
            fail(f'{name}: envelope signs {image_bytes} bytes, the window is {WINDOWS[name]}')
        if data[120:152] != anchor_digest:
            fail(f'{name}: envelope was signed under a different anchor than {ANCHOR.name}')
        envelopes.append((name, path, data))
        records.append((name, image_bytes))

    objects = []
    for source in (KIT / 'src/verified_root.c', KIT / 'adapters/xnu_crypto.c'):
        if not source.is_file():
            fail(f'missing {source}')
        destination = OUT / (source.stem + '.o')
        command, index = [], 0
        while index < len(record['arguments']):
            value = record['arguments'][index]
            if value == record['file']:
                command.append(str(source))
            elif value == '-o':
                command += ['-o', str(destination)]
                index += 1
            elif value == '-Xclang' and index + 1 < len(record['arguments']) \
                    and record['arguments'][index + 1].startswith('-W'):
                index += 1
            elif not value.startswith('-W'):
                command.append(value)
            index += 1
        command.extend(['-Wno-error', '-I', str(KIT / 'include')])
        (OUT / (source.stem + '.compile-command.json')).write_text(json.dumps(command, indent=2) + '\n')
        result = subprocess.run(command, cwd=record['directory'], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        (OUT / (source.stem + '.compile.log')).write_text(result.stdout or '')
        if result.returncode:
            fail(f'{source.name} did not compile:\n' + (result.stdout or '')[-3000:])
        objects.append(destination)

    assembly = OUT / 'verified-root-policies.S'
    text = ['// Generated inputs: the two owner-signed envelopes, the anchor and the table that',
            '// NativeUfs selects from. Never edited by hand.',
            '.section __DATA_CONST,__const']
    for name, path, data in envelopes:
        text += ['.balign 8', f'.globl _verified_root_{name.lower()}_manifest',
                 f'_verified_root_{name.lower()}_manifest:',
                 f'.incbin "{path.as_posix()}"',
                 '.balign 8', f'.globl _verified_root_{name.lower()}_manifest_bytes',
                 f'_verified_root_{name.lower()}_manifest_bytes:',
                 f'.quad {len(data)}']
    text += ['.balign 8', '.globl _verified_root_key', '_verified_root_key:',
             f'.incbin "{ANCHOR.as_posix()}"',
             '.balign 8', '.globl _verified_root_policies, _verified_root_policy_count',
             '_verified_root_policies:']
    for name, _, data in envelopes:
        text.append(f'.quad _verified_root_{name.lower()}_manifest, {len(data)}')
    text += ['_verified_root_policy_count:', f'.quad {len(envelopes)}']
    assembly.write_text('\n'.join(text) + '\n')

    destination = OUT / 'verified-root-policies.o'
    assemble = [record['arguments'][0]]
    for flag in ('-target', '-arch'):
        if flag in record['arguments']:
            at = record['arguments'].index(flag)
            assemble += record['arguments'][at:at + 2]
    assemble += ['-c', str(assembly), '-o', str(destination)]
    result = subprocess.run(assemble, cwd=record['directory'], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    (OUT / 'assemble.log').write_text(result.stdout or '')
    if result.returncode:
        fail('the policy assembly did not assemble:\n' + (result.stdout or '')[-3000:])
    objects.append(destination)

    archive = OUT / 'libVerifiedRootBundle.a'
    archive.unlink(missing_ok=True)
    subprocess.run(['xcrun', 'ar', 'rcs', str(archive)] + [str(o) for o in objects], check=True)

    needed = {'_vr_open', '_vr_verify_block', '_vr_xnu_crypto', '_verified_root_key',
              '_verified_root_policies', '_verified_root_policy_count'}
    symbols = subprocess.run(['xcrun', 'nm', '-g', str(archive)], capture_output=True, text=True, check=True).stdout
    defined = {line.split()[-1] for line in symbols.splitlines() if len(line.split()) >= 3 and line.split()[-2] != 'U'}
    missing = needed - defined
    if missing:
        fail('the archive lacks ' + ', '.join(sorted(missing)))

    # The definitions belong in the archive NativeUfs.o already lives in: an archive
    # member is pulled in to satisfy a reference from a sibling member, whereas a
    # separate archive on the kernel link line was tried twice and produced undefined
    # symbols both times (see build-verified-root-stub.py, which does the same merge).
    ufs = ROOT / 'build/native-ufs/libNativeUfs.a'
    if not ufs.is_file():
        fail('build/native-ufs/libNativeUfs.a is not built yet; this archive entry has to come after '
             '"build-arm-driver.py NativeUfs" in the board list')
    subprocess.run(['xcrun', 'ar', 'rs', str(ufs)] + [str(o) for o in objects], check=True)
    merged_symbols = subprocess.run(['xcrun', 'nm', '-g', str(ufs)], capture_output=True, text=True, check=True).stdout
    merged = {line.split()[-1] for line in merged_symbols.splitlines()
              if len(line.split()) >= 3 and line.split()[-2] != 'U'}
    absent = needed - merged
    if absent:
        fail('libNativeUfs.a still lacks ' + ', '.join(sorted(absent)))

    print('VerifiedRootBundle: %d policies (%s) under anchor %s' %
          (len(envelopes), ', '.join('%s %d B' % (n, b) for n, b in records),
           hashlib.sha256(anchor).hexdigest()[:16]))
    print('VerifiedRootBundle: %s (%d bytes), merged into %s' %
          (archive.name, archive.stat().st_size, ufs.relative_to(ROOT)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
