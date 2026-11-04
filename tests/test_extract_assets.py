"""The Qt resource reader behind emulator/extract_assets.py, checked against a
hand-built blob in the rcc v1 layout that Qt 4.8 compiles into the panel
binary. No firmware is needed to run these."""
import struct
import zlib

import pytest

from emulator.extract_assets import read_resources


def _name(text):
    encoded = text.encode('utf-16-be')
    return struct.pack('>HI', len(text), 0) + encoded


def _dir(name_off, count, first):
    return struct.pack('>IHII', name_off, 0x02, count, first)


def _file(name_off, data_off, compressed=False):
    return struct.pack('>IHHHI', name_off, 0x01 if compressed else 0x00, 0, 0, data_off)


def _blob():
    # Tree: / -> Weather/ -> {1_sunny.png (plain), 5_rain.png (qCompress'd)}
    names = b''
    offsets = {}
    for n in ('Weather', '1_sunny.png', '5_rain.png'):
        offsets[n] = len(names)
        names += _name(n)

    sunny = b'\x89PNG sunny'
    rain = b'\x89PNG rain' * 20
    packed_rain = struct.pack('>I', len(rain)) + zlib.compress(rain)
    data = struct.pack('>I', len(sunny)) + sunny
    rain_off = len(data)
    data += struct.pack('>I', len(packed_rain)) + packed_rain

    tree = (
        _dir(0, 1, 1)                                  # 0: root
        + _dir(offsets['Weather'], 2, 2)               # 1: Weather/
        + _file(offsets['1_sunny.png'], 0)             # 2
        + _file(offsets['5_rain.png'], rain_off, True)  # 3
    )
    return tree, names, data, sunny, rain


def test_reads_plain_and_compressed_files_with_full_paths():
    tree, names, data, sunny, rain = _blob()
    assert dict(read_resources(tree, names, data)) == {
        'Weather/1_sunny.png': sunny,
        'Weather/5_rain.png': rain,
    }


def test_rejects_a_tree_that_points_outside_the_name_table():
    tree, names, data, *_ = _blob()
    with pytest.raises(ValueError):
        list(read_resources(tree, names[:4], data))
