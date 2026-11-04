"""
Pulls the panel UI's images out of the firmware for the web emulator.

The tuxedo binary (Qt 4.8) has every image compiled in as a Qt resource. Its
symbol table still names the three rcc arrays (qt_resource_struct, _name,
_data), so this walks the real resource tree and writes each file under its
original path, e.g. emulator/assets/Weather/LoungeLizard/Weather/1_sunny.png.

The images are Honeywell's. They're written to emulator/assets/, which is
gitignored, and must not be committed or published.

Usage: python -m emulator.extract_assets [BINARY] [OUT_DIR]
"""
import os
import struct
import sys
import zlib

BINARY = 'TUXW_V5.3.21.0_CN/_app2.hdr.extracted/jffs2-root/tuxedo'
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'assets')

RESOURCE_SYMBOLS = ('qt_resource_struct', 'qt_resource_name', 'qt_resource_data')

# rcc v1 tree node: 14 bytes, big-endian. Node 0 is the root directory.
NODE_SIZE = 14
FLAG_COMPRESSED = 0x01
FLAG_DIRECTORY = 0x02


def _name_at(names: bytes, offset: int) -> str:
    if offset + 6 > len(names):
        raise ValueError(f'name offset {offset:#x} is outside the name table')
    (length,) = struct.unpack_from('>H', names, offset)
    start = offset + 6  # skip the length and the 4-byte hash
    if start + length * 2 > len(names):
        raise ValueError(f'name at {offset:#x} runs past the name table')
    return names[start:start + length * 2].decode('utf-16-be')


def _data_at(data: bytes, offset: int, compressed: bool) -> bytes:
    (size,) = struct.unpack_from('>I', data, offset)
    payload = data[offset + 4:offset + 4 + size]
    if compressed:
        # qCompress(): 4-byte big-endian uncompressed size, then a zlib stream.
        payload = zlib.decompress(payload[4:])
    return payload


def read_resources(tree: bytes, names: bytes, data: bytes):
    """Yield (path, bytes) for every file in an rcc v1 resource tree."""
    def walk(node, prefix):
        offset = node * NODE_SIZE
        name_off, flags = struct.unpack_from('>IH', tree, offset)
        name = _name_at(names, name_off) if node else ''
        path = f'{prefix}/{name}' if prefix else name
        if flags & FLAG_DIRECTORY:
            count, first = struct.unpack_from('>II', tree, offset + 6)
            for child in range(first, first + count):
                yield from walk(child, path)
        else:
            (data_off,) = struct.unpack_from('>I', tree, offset + 10)
            yield path, _data_at(data, data_off, bool(flags & FLAG_COMPRESSED))

    yield from walk(0, '')


def _elf_symbols(elf: bytes, wanted) -> dict:
    """Map each wanted symbol to (file offset, size) via the ELF32 .symtab."""
    if elf[:4] != b'\x7fELF' or elf[4] != 1:
        raise ValueError('not a 32-bit ELF file')
    shoff, = struct.unpack_from('<I', elf, 0x20)
    shentsize, shnum = struct.unpack_from('<HH', elf, 0x2e)
    sections = [struct.unpack_from('<IIIIIIIIII', elf, shoff + i * shentsize)
                for i in range(shnum)]

    found = {}
    for sh_type, sh_offset, sh_size, sh_link in (
            (s[1], s[4], s[5], s[6]) for s in sections):
        if sh_type != 2:  # SHT_SYMTAB
            continue
        strtab = sections[sh_link][4]
        for pos in range(sh_offset, sh_offset + sh_size, 16):
            st_name, st_value, st_size, _, _, st_shndx = struct.unpack_from(
                '<IIIBBH', elf, pos)
            end = elf.index(b'\0', strtab + st_name)
            name = elf[strtab + st_name:end].decode('ascii', 'replace')
            if name in wanted and 0 < st_shndx < shnum:
                # Virtual address -> file offset through the owning section.
                sec = sections[st_shndx]
                found[name] = (st_value - sec[3] + sec[4], st_size)

    missing = set(wanted) - set(found)
    if missing:
        raise ValueError(f'symbols not found: {", ".join(sorted(missing))}')
    return found


def extract(binary: str, out_dir: str) -> int:
    with open(binary, 'rb') as f:
        elf = f.read()
    symbols = _elf_symbols(elf, RESOURCE_SYMBOLS)
    tree, names, data = (elf[off:off + size]
                         for off, size in (symbols[s] for s in RESOURCE_SYMBOLS))

    count = 0
    for path, content in read_resources(tree, names, data):
        target = os.path.join(out_dir, path)
        if not os.path.abspath(target).startswith(os.path.abspath(out_dir) + os.sep):
            raise ValueError(f'resource path escapes the output folder: {path}')
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'wb') as f:
            f.write(content)
        count += 1
    return count


def main():
    binary = sys.argv[1] if len(sys.argv) > 1 else BINARY
    out_dir = sys.argv[2] if len(sys.argv) > 2 else OUTPUT_DIR
    if not os.path.exists(binary):
        print(f'Binary not found: {binary}')
        print('Unpack the firmware with binwalk and jefferson first (see emulator/README.md).')
        return 1
    count = extract(binary, out_dir)
    print(f'Wrote {count} resources to {out_dir}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
