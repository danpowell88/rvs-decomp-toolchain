#!/usr/bin/env python
"""Builds the toolchain staging folder: assets/<piece>.zip, files/<piece>.tsv and manifest.json.

Run with the interpreter in isolated mode:  python -I stage.py --help

For every piece it splits the files into
  * public  : byte-identical (same relative path, same SHA-256) to a file inside a pinned
              public archive -> fetched from that archive by URL;
  * release : everything else -> packed into assets/<piece>.zip (published as the assets of
              release toolchain-1; files lists mark these rows `private`).
Nothing from an untrusted archive is executed; archives are only hashed.
"""
import argparse
import hashlib
import json
import os
import sys
import zipfile

ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def walk(root):
    out = {}
    for dp, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(dp, f)
            out[os.path.relpath(p, root).replace(os.sep, '/')] = p
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--old', required=True, help='old tools/toolchain directory')
    ap.add_argument('--nvdxt', required=True, help='directory with dxtlib.h and nvdxt_options.h')
    ap.add_argument('--pub', required=True,
                    help='directory holding the extracted public archives (subdirs omniblade-msvc7.0, '
                         'omniblade-msvc6.5pp, stlport-4.5.3) and the archives themselves (dl/)')
    ap.add_argument('--out', required=True, help='staging folder')
    ap.add_argument('--manifest-copy', required=True, help='where to write the repo copy of manifest.json')
    ap.add_argument('--files-dir', required=True, help='where to write files/<piece>.tsv (repo copy)')
    a = ap.parse_args()

    publics = {
        'omniblade-msvc7.0': dict(
            url='https://github.com/OmniBlade/decomp.me/releases/download/msvcwin9x/msvc7.0.tar.gz',
            archive='msvc7.0.tar.gz', tree=os.path.join(a.pub, 'x', 'msvc7.0'), strip=''),
        'omniblade-msvc6.5pp': dict(
            url='https://github.com/OmniBlade/decomp.me/releases/download/msvcwin9x/msvc6.5pp.tar.gz',
            archive='msvc6.5pp.tar.gz', tree=os.path.join(a.pub, 'x', 'msvc6.5pp'), strip=''),
        'stlport-4.5.3-sourceforge': dict(
            url='https://downloads.sourceforge.net/project/stlport/STLport%20archive/STLport%204/STLport-4.5.3.tar.gz',
            archive='STLport-4.5.3.tar.gz', tree=os.path.join(a.pub, 'x', 'STLport-4.5.3'), strip=''),
    }
    for pid, p in publics.items():
        p['sha256'] = sha256(os.path.join(a.pub, 'dl', p['archive']))
        p['size'] = os.path.getsize(os.path.join(a.pub, 'dl', p['archive']))
        p['index'] = {k.lower(): (k, v) for k, v in walk(p['tree']).items()}

    # piece: (layout dir, source root, include predicate, dest-relative mapper, public archive id,
    #         public-path mapper (dest rel path -> archive rel path))
    old = a.old
    pieces = []

    def piece(pid, layout, desc, files, pub=None, pubmap=None):
        pieces.append(dict(id=pid, layout=layout, desc=desc, files=files, pub=pub, pubmap=pubmap))

    def subtree(root, prefix='', skip=lambda rel: False):
        d = {}
        for rel, p in walk(root).items():
            if not skip(rel):
                d[prefix + rel] = p
        return d

    # msvc71: CRT headers and import/static libs of the VC++ Toolkit 2003. The 3052 compiler in its bin/ is
    # not used (the retail 13.10.3077 compiler comes from msvc71-3077), so bin/ is not shipped.
    piece('msvc71', 'msvc71', 'MSVC 7.1 (VC++ Toolkit 2003) CRT headers and libraries',
          subtree(os.path.join(old, 'msvc71'), skip=lambda r: r.startswith('bin/')))
    piece('msvc71-3077', 'msvc71-3077',
          'cl 13.10.3077 (VS .NET 2003) compiler, linker and tools: the retail build',
          subtree(os.path.join(old, 'msvc71-3077'), skip=lambda r: r == 'README.md'),
          pub='omniblade-msvc7.0')
    piece('vc6sp5pp', 'vc6sp5pp',
          'VC++ 6.0 SP5 + Processor Pack (cl 12.00.9044): Karma, ubi.com GS SDK, NvTriStrip, zlib',
          subtree(os.path.join(old, 'vc6sp5pp'), skip=lambda r: r == 'README.md'),
          pub='omniblade-msvc6.5pp')
    piece('winsdk', 'winsdk', 'Windows Server 2003 SP1 Platform SDK (x86 Include and Lib)',
          subtree(os.path.join(old, 'winsdk'), skip=lambda r: r == 'README.md'))
    piece('dxsdk', 'dxsdk', 'DirectX SDK headers and libraries (DX8-era)',
          subtree(os.path.join(old, 'dxsdk')))
    piece('stlport-4.5.3', 'stlport-4.5.3', 'STLport 4.5.3 headers (used for the ubi.com GS SDK rebuild)',
          subtree(os.path.join(old, 'stlport-4.5.3'), skip=lambda r: r == 'VENDOR_README.md'),
          pub='stlport-4.5.3-sourceforge', pubmap=lambda rel: 'STLport-4.5.3/' + rel)
    impl = {}
    impl.update(subtree(os.path.join(old, 'snd_implib'), 'snd_implib/'))
    impl.update(subtree(os.path.join(old, 'vorbis_implib'), 'vorbis_implib/'))
    piece('implibs', '.', 'Import-library definitions for the SND DLL twins and vorbisfile', impl)
    piece('vendor-nvdxt', 'vendor/nvdxt', 'NVIDIA nvDXT headers (dxtlib.h, nvdxt_options.h)',
          subtree(a.nvdxt, 'Inc/'))

    os.makedirs(os.path.join(a.out, 'assets'), exist_ok=True)
    os.makedirs(a.files_dir, exist_ok=True)
    man_pieces = []
    total_private = 0
    summary = []
    for pc in pieces:
        rows = []
        priv = []
        pubfiles = 0
        pubbytes = 0
        for rel in sorted(pc['files'], key=lambda s: s.lower()):
            src = pc['files'][rel]
            h = sha256(src)
            size = os.path.getsize(src)
            frm = ''
            kind = 'private'
            if pc['pub']:
                pa = publics[pc['pub']]
                key = (pc['pubmap'](rel) if pc['pubmap'] else rel).lower()
                hit = pa['index'].get(key)
                if hit and sha256(hit[1]) == h:
                    kind = 'public'
                    frm = hit[0]
            rows.append((h, size, rel, kind, frm))
            if kind == 'public':
                pubfiles += 1
                pubbytes += size
            else:
                priv.append((rel, src, size))
        tsv_path = os.path.join(a.files_dir, pc['id'] + '.tsv')
        with open(tsv_path, 'w', newline='\n') as f:
            for h, size, rel, kind, frm in rows:
                f.write('%s\t%d\t%s\t%s\t%s\n' % (h, size, rel, kind, frm))
        ent = dict(id=pc['id'], description=pc['desc'], layout=pc['layout'],
                   files_list='files/%s.tsv' % pc['id'], files_list_sha256=sha256(tsv_path),
                   file_count=len(rows), bytes=sum(r[1] for r in rows))
        if pubfiles:
            pa = publics[pc['pub']]
            ent['public'] = dict(id=pc['pub'], url=pa['url'], archive_sha256=pa['sha256'],
                                 archive_size=pa['size'], archive_name=pa['archive'],
                                 files=pubfiles, bytes=pubbytes)
        if priv:
            zp = os.path.join(a.out, 'assets', pc['id'] + '.zip')
            with zipfile.ZipFile(zp, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
                for rel, src, size in priv:
                    zi = zipfile.ZipInfo(rel, ZIP_DATE)
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    zi.external_attr = 0o644 << 16
                    with open(src, 'rb') as fh:
                        z.writestr(zi, fh.read(), zipfile.ZIP_DEFLATED, 9)
            ent['release'] = dict(asset='assets/%s.zip' % pc['id'], sha256=sha256(zp),
                                  size=os.path.getsize(zp), files=len(priv),
                                  bytes=sum(p[2] for p in priv))
            total_private += os.path.getsize(zp)
        man_pieces.append(ent)
        summary.append((pc['id'], len(rows), ent['bytes'], pubfiles, pubbytes, len(priv),
                        ent.get('release', {}).get('size', 0)))
    manifest = dict(
        schema=1,
        release_repo='danpowell88/rvs-decomp-toolchain',
        release_tag='toolchain-1',
        assets_dir='assets',
        pieces=man_pieces)
    for dest in (os.path.join(a.out, 'manifest.json'), a.manifest_copy):
        with open(dest, 'w', newline='\n') as f:
            json.dump(manifest, f, indent=1)
            f.write('\n')
    print('%-20s %6s %10s | %6s %10s | %6s %10s' % ('piece', 'files', 'bytes', 'pubN', 'pubB', 'privN', 'zipB'))
    for s in summary:
        print('%-20s %6d %10d | %6d %10d | %6d %10d' % s)
    print('total release zip bytes:', total_private)


if __name__ == '__main__':
    sys.exit(main())
