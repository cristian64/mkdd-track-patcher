"""
Module containing functions for unpacking and packing the BAA files that are present in some
GameCube games such as Mario Kart: Double Dash!!
"""
import json
import os
import struct

from enum import IntEnum


def read_uint32(f):
    return struct.unpack(">I", f.read(4))[0]


def write_uint32(f, value):
    f.write(struct.pack(">I", value))


def prepare_destination_directory(dirpath: str):
    if not os.path.exists(dirpath):
        os.makedirs(dirpath)
    elif not os.path.isdir(dirpath):
        raise RuntimeError(
            f'Destination directory "{dirpath}" is not a valid directory and cannot be created')
    else:
        with os.scandir(dirpath) as it:
            if any(it):
                raise RuntimeError(f'Destination directory "{dirpath}" is not empty')


def aligned(value: int, alignment: int) -> int:
    return (value | alignment - 1) + 1 if value % alignment else value


class SectionType(IntEnum):
    BAAC = 0x62616163  # BINARY AUDIO ARCHIVE CUSTOM
    BFCA = 0x62666361
    BMS = 0x626D7320  # BINARY MUSIC SEQUENCE
    BNK = 0x626E6B20  # INSTRUMENT BANK
    BSC = 0x62736320  # BINARY SEQUENCE COLLECTION
    BSFT = 0x62736674  # BINARY STREAM FILE TABLE
    BST = 0x62737420  # BINARY SOUND TABLE
    BSTN = 0x6273746E  # BINARY SOUND TABLE NAME
    WSYS = 0x77732020  # WAVE SYSTEM


FILE_EXTENSIONS = {
    SectionType.BAAC: '.baac',
    SectionType.BFCA: '.bfca',
    SectionType.BMS: '.bms',
    SectionType.BNK: '.bnk',
    SectionType.BSC: '.bsc',
    SectionType.BSFT: '.bsft',
    SectionType.BST: '.bst',
    SectionType.BSTN: '.bstn',
    SectionType.WSYS: '.wsy',
}

BAA_MAGIC = 0x41415F3C
BAA_FOOTER = 0x3E5F4141


def parse_baa_header(f) -> list[dict]:
    sections = []

    magic = read_uint32(f)
    if magic != BAA_MAGIC:
        raise RuntimeError(f'Bad magic in BAA file: 0x{magic:08X} (expected 0x{BAA_MAGIC:08X})')

    while True:
        section_type = read_uint32(f)
        if section_type == BAA_FOOTER:
            break

        try:
            section_type = SectionType(section_type)
        except ValueError as e:
            raise RuntimeError(f'Unexpected file type in BAA file: 0x{section_type:08X}') from e

        section = {'type': section_type}

        if section_type == SectionType.BST:
            section['start'] = read_uint32(f)
            section['end'] = read_uint32(f)
        elif section_type == SectionType.BSTN:
            section['start'] = read_uint32(f)
            section['end'] = read_uint32(f)
        elif section_type == SectionType.WSYS:
            section['number'] = read_uint32(f)
            section['start'] = read_uint32(f)
            section['flags'] = read_uint32(f)
        elif section_type == SectionType.BNK:
            section['number'] = read_uint32(f)
            section['start'] = read_uint32(f)
        elif section_type == SectionType.BSC:
            section['start'] = read_uint32(f)
            section['end'] = read_uint32(f)
        elif section_type == SectionType.BMS:
            section['number'] = read_uint32(f)
            section['start'] = read_uint32(f)
            section['end'] = read_uint32(f)
        elif section_type == SectionType.BSFT:
            section['start'] = read_uint32(f)
        elif section_type == SectionType.BFCA:
            section['start'] = read_uint32(f)
        elif section_type == SectionType.BAAC:
            section['start'] = read_uint32(f)
            section['end'] = read_uint32(f)

        sections.append(section)

    return sections


def get_baa_section_size(section: dict, f) -> int:
    section_type = section['type']
    section_start = section['start']

    if section_type == SectionType.BNK:
        f.seek(section_start + 4)  # +4 to skip IBNK magic.
        return read_uint32(f)

    if section_type == SectionType.WSYS:
        f.seek(section_start + 4)  # +4 to skip WSYS magic.
        return read_uint32(f)

    if section_type == SectionType.BSFT:
        f.seek(section_start + 4)  # +4 to skip BSFT magic.
        # The only way to figure out the size is to find the offset to the last string in the table,
        # and check how long that string is.
        string_count = read_uint32(f)
        max_string_offset = 4 + 4 + 4 * string_count  # Magic + count field + offsets table.
        for _ in range(string_count):
            string_offset = read_uint32(f)
            max_string_offset = max(max_string_offset, string_offset)
        size = max_string_offset
        if string_count:
            f.seek(max_string_offset + section_start)
            while f.read(1) != b'\0':
                size += 1
            size += 1  # Null character.
        return size

    if section_type == SectionType.BFCA:
        raise RuntimeError('Unable to calculate size for unknown BFCA type')

    return section['end'] - section_start


def unpack_baa(src_filepath: str, dst_dirpath: str):
    assert src_filepath.endswith('.baa')

    prepare_destination_directory(dst_dirpath)

    with open(src_filepath, 'rb') as f:
        sections = parse_baa_header(f)

        for i, section in enumerate(sections):
            filename = f'{i}{FILE_EXTENSIONS[section["type"]]}'
            filepath = os.path.join(dst_dirpath, filename)

            section_size = get_baa_section_size(section, f)
            f.seek(section['start'])
            data = f.read(section_size)

            with open(filepath, 'wb') as output_file:
                output_file.write(data)

    filename = os.path.basename(src_filepath)
    filepath = os.path.join(dst_dirpath, f'{filename}_info.json')

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(json.dumps(sections, indent=4))


def pack_baa(src_filepath: str, dst_filepath: str):
    assert src_filepath.endswith('.baa_info.json')
    assert dst_filepath.endswith('.baa')

    with open(src_filepath, 'r', encoding='utf-8') as input_file:
        sections = json.load(input_file)

    src_dirpath = os.path.dirname(src_filepath)

    with open(dst_filepath, 'wb') as output_file:
        write_uint32(output_file, BAA_MAGIC)

        file_data_blobs = []

        for i, section in enumerate(sections):
            section_type = SectionType(section['type'])
            write_uint32(output_file, section_type)

            if 'number' in section:
                write_uint32(output_file, section['number'])

            start_offset_offset = output_file.tell()
            write_uint32(output_file, 0xBAAAAAAD)

            if 'end' in section:
                end_offset_offset = output_file.tell()
                write_uint32(output_file, 0xBAAAAAAD)
            else:
                end_offset_offset = None

            if 'flags' in section:
                write_uint32(output_file, section['flags'])

            filename = f'{i}{FILE_EXTENSIONS[section_type]}'
            filepath = os.path.join(src_dirpath, filename)

            with open(filepath, 'rb') as input_file:
                data = input_file.read()

            file_data_blobs.append((
                section['start'],
                data,
                start_offset_offset,
                end_offset_offset,
                section_type,
            ))

        write_uint32(output_file, BAA_FOOTER)

        file_data_blobs.sort()  # Sort by the original start offsets.

        for (
                _original_start_offset,
                data,
                start_offset_offset,
                end_offset_offset,
                section_type,
        ) in file_data_blobs:
            start_offset = output_file.tell()
            output_file.write(data)
            end_offset = output_file.tell()

            # It was observed in GCKart.baa that certain types are aligned. MKDD does not seem to
            # care about this alignment, but adding it enables the tool to reconstruct GCKart.baa
            # identically.
            if section_type == SectionType.BNK:
                alignment = 16
            elif section_type == SectionType.WSYS:
                alignment = 32
            else:
                alignment = 0
            if alignment:
                padding = aligned(end_offset, alignment) - end_offset
                output_file.write(b'\x00' * padding)

            cursor = output_file.tell()

            output_file.seek(start_offset_offset)
            write_uint32(output_file, start_offset)

            if end_offset_offset is not None:
                output_file.seek(end_offset_offset)
                write_uint32(output_file, end_offset)

            output_file.seek(cursor)


def unpack_baac(src_filepath: str, dst_dirpath: str):
    assert src_filepath.endswith('.baac')

    prepare_destination_directory(dst_dirpath)

    with open(src_filepath, 'rb') as input_file:
        file_count = read_uint32(input_file)
        offsets = tuple(read_uint32(input_file) for _ in range(file_count))

        filename_padding = len(str(file_count))

        for i, offset in enumerate(offsets):
            if i + 1 < len(offsets):
                size = offsets[i + 1] - offset
                data = input_file.read(size)
            else:
                data = input_file.read()  # To the end of the file.

            filename = f'{i:0{filename_padding}}.baa'
            filepath = os.path.join(dst_dirpath, filename)
            with open(filepath, 'wb') as output_file:
                output_file.write(data)


def pack_baac(src_filepaths: list[str], dst_filepath: str):
    for src_filepath in src_filepaths:
        assert src_filepath.endswith('.baa')
    assert dst_filepath.endswith('.baac')

    with open(dst_filepath, 'wb') as output_file:
        write_uint32(output_file, len(src_filepaths))

        for _ in range(len(src_filepaths)):
            write_uint32(output_file, 0)  # Offset placeholder; they will be updated.

        offsets = []
        for src_filepath in src_filepaths:
            with open(src_filepath, 'rb') as input_file:
                data = input_file.read()
            offsets.append(output_file.tell())
            output_file.write(data)

        output_file.seek(4)
        for offset in offsets:
            write_uint32(output_file, offset)


def read_bsft(src_filepath: str) -> list[tuple[int, str]]:
    assert src_filepath.endswith('.bsft')

    with open(src_filepath, 'rb') as f:
        magic = f.read(4)
        assert magic == b'bsft'
        string_count = read_uint32(f)
        string_offsets = tuple(read_uint32(f) for _ in range(string_count))

        offsets_and_strings = []
        for string_offset in string_offsets:
            f.seek(string_offset)
            string = bytearray()
            while (value := f.read(1)) != b'\0':
                string += value
            offsets_and_strings.append((string_offset, bytes(string).decode(encoding='ascii')))

    return offsets_and_strings


def write_bsft(strings: list[str], dst_filepath: str):
    assert dst_filepath.endswith('.bsft')

    with open(dst_filepath, 'wb') as f:
        f.write(b'bsft')
        write_uint32(f, len(strings))

        for _ in range(len(strings)):
            write_uint32(f, 0)  # Offset placeholder; it will be updated.

        string_offsets = []
        for string in strings:
            string_offsets.append(f.tell())
            f.write(bytes(string, encoding='ascii'))
            f.write(b'\x00')  # Null character.

        f.seek(4 + 4)  # After file magic and string count.
        for string_offset in string_offsets:
            write_uint32(f, string_offset)
