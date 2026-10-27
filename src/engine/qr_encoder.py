"""Pure-Python ISO/IEC 18004 QR Code Matrix Generator (Byte mode, ECC Level L, Versions 1-6).

Generates standard scannable QR code matrices (2D list of 0/1 modules) with zero external
dependencies so the Dashboard can render real, phone-camera-scannable QR codes offline.
"""

from typing import List, Tuple

# Version capabilities for ECC Level L (total codewords, ec codewords per block, num blocks, max byte capacity)
_VERSION_TABLE = {
    1: (26, 7, 1, 17, []),
    2: (44, 10, 1, 32, [6, 18]),
    3: (70, 15, 1, 53, [6, 22]),
    4: (100, 20, 1, 78, [6, 26]),
    5: (134, 26, 1, 106, [6, 30]),
    6: (172, 18, 2, 134, [6, 34]),
}

# Precomputed format info strings (15 bits) for ECC Level L (01) and Mask Patterns 0..7
_FORMAT_BITS_L = [
    0x77C4, 0x72F3, 0x7DAA, 0x789D,
    0x662F, 0x6318, 0x6C41, 0x6976,
]


def _gf_tables() -> Tuple[List[int], List[int]]:
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_GF_EXP, _GF_LOG = _gf_tables()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator_poly(degree: int) -> List[int]:
    poly = [1]
    for i in range(degree):
        next_poly = [0] * (len(poly) + 1)
        factor = _GF_EXP[i]
        for j, coeff in enumerate(poly):
            next_poly[j] ^= coeff
            next_poly[j + 1] ^= _gf_mul(coeff, factor)
        poly = next_poly
    return poly


def _rs_encode(data: List[int], ec_len: int) -> List[int]:
    gen = _rs_generator_poly(ec_len)
    rem = [0] * ec_len
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        if factor != 0:
            for i in range(ec_len):
                rem[i] ^= _gf_mul(gen[i + 1], factor)
    return rem


def _select_version(byte_len: int) -> int:
    for ver, (_, _, _, max_bytes, _) in sorted(_VERSION_TABLE.items()):
        if byte_len <= max_bytes:
            return ver
    raise ValueError(f"QR payload too long ({byte_len} bytes; max 134 bytes for Version 6-L).")


def _encode_data_codewords(payload_bytes: bytes, version: int) -> List[int]:
    total_cw, ec_cw_per_block, num_blocks, _, _ = _VERSION_TABLE[version]
    data_cw_count = total_cw - ec_cw_per_block * num_blocks

    bits: List[int] = []

    def append_bits(val: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            bits.append((val >> i) & 1)

    # Byte mode indicator (0100) + 8-bit character count (for versions 1..9)
    append_bits(0b0100, 4)
    append_bits(len(payload_bytes), 8)
    for b in payload_bytes:
        append_bits(b, 8)

    # Terminator (up to 4 zero bits)
    max_bits = data_cw_count * 8
    term_len = min(4, max_bits - len(bits))
    append_bits(0, term_len)

    # Pad to byte boundary
    while len(bits) % 8 != 0:
        bits.append(0)

    codewords: List[int] = []
    for i in range(0, len(bits), 8):
        byte_val = 0
        for bit in bits[i:i + 8]:
            byte_val = (byte_val << 1) | bit
        codewords.append(byte_val)

    # Pad codewords alternating 0xEC, 0x11
    pad_bytes = [0xEC, 0x11]
    pad_idx = 0
    while len(codewords) < data_cw_count:
        codewords.append(pad_bytes[pad_idx % 2])
        pad_idx += 1

    # Split into blocks and interleave if num_blocks > 1
    block_size = data_cw_count // num_blocks
    data_blocks: List[List[int]] = []
    ec_blocks: List[List[int]] = []
    for b_idx in range(num_blocks):
        block = codewords[b_idx * block_size:(b_idx + 1) * block_size]
        data_blocks.append(block)
        ec_blocks.append(_rs_encode(block, ec_cw_per_block))

    final_codewords: List[int] = []
    for i in range(block_size):
        for b_idx in range(num_blocks):
            final_codewords.append(data_blocks[b_idx][i])
    for i in range(ec_cw_per_block):
        for b_idx in range(num_blocks):
            final_codewords.append(ec_blocks[b_idx][i])

    return final_codewords


def encode_qr_matrix(text: str) -> List[List[int]]:
    """
    Encode `text` into a standard ISO/IEC 18004 QR Code matrix.
    Returns a 2D square matrix of 0 (light) and 1 (dark) ints.
    """
    raw = text.encode("utf-8")
    version = _select_version(len(raw))
    size = 17 + 4 * version
    _, _, _, _, align_coords = _VERSION_TABLE[version]

    matrix: List[List[int]] = [[0] * size for _ in range(size)]
    is_function: List[List[bool]] = [[False] * size for _ in range(size)]

    def set_func_module(r: int, c: int, dark: bool) -> None:
        if 0 <= r < size and 0 <= c < size:
            matrix[r][c] = 1 if dark else 0
            is_function[r][c] = True

    def draw_finder(top_r: int, left_c: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = top_r + dr, left_c + dc
                if 0 <= r < size and 0 <= c < size:
                    in_outer = 0 <= dr <= 6 and 0 <= dc <= 6
                    on_border = dr in (0, 6) or dc in (0, 6)
                    in_center = 2 <= dr <= 4 and 2 <= dc <= 4
                    set_func_module(r, c, in_outer and (on_border or in_center))

    draw_finder(0, 0)
    draw_finder(0, size - 7)
    draw_finder(size - 7, 0)

    # Timing patterns
    for i in range(8, size - 8):
        set_func_module(6, i, i % 2 == 0)
        set_func_module(i, 6, i % 2 == 0)

    # Alignment patterns
    for ar in align_coords:
        for ac in align_coords:
            if (ar < 9 and ac < 9) or (ar < 9 and ac > size - 10) or (ar > size - 10 and ac < 9):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    dark = max(abs(dr), abs(dc)) in (0, 2)
                    set_func_module(ar + dr, ac + dc, dark)

    # Dark module & reserved format areas
    set_func_module(size - 8, 8, True)
    for i in range(9):
        if not is_function[8][i]:
            set_func_module(8, i, False)
        if not is_function[i][8]:
            set_func_module(i, 8, False)
    for i in range(8):
        if not is_function[8][size - 1 - i]:
            set_func_module(8, size - 1 - i, False)
        if not is_function[size - 1 - i][8]:
            set_func_module(size - 1 - i, 8, False)

    # Place data bits in upward/downward 2-column zigzag
    codewords = _encode_data_codewords(raw, version)
    bit_stream: List[int] = []
    for cw in codewords:
        for shift in range(7, -1, -1):
            bit_stream.append((cw >> shift) & 1)

    bit_idx = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        row_range = range(size - 1, -1, -1) if upward else range(size)
        for row in row_range:
            for dc in (0, -1):
                c = col + dc
                if not is_function[row][c]:
                    bit = bit_stream[bit_idx] if bit_idx < len(bit_stream) else 0
                    bit_idx += 1
                    # Apply Mask 0: (row + c) % 2 == 0
                    if (row + c) % 2 == 0:
                        bit ^= 1
                    matrix[row][c] = bit
        upward = not upward
        col -= 2

    # Write Format Information for Mask 0, ECC Level L
    fmt = _FORMAT_BITS_L[0]
    for i in range(6):
        set_func_module(8, i, bool((fmt >> i) & 1))
    set_func_module(8, 7, bool((fmt >> 6) & 1))
    set_func_module(8, 8, bool((fmt >> 7) & 1))
    set_func_module(7, 8, bool((fmt >> 8) & 1))
    for i in range(9, 15):
        set_func_module(14 - i, 8, bool((fmt >> i) & 1))

    for i in range(8):
        set_func_module(size - 1 - i, 8, bool((fmt >> i) & 1))
    for i in range(8, 15):
        set_func_module(8, size - 15 + i, bool((fmt >> i) & 1))

    return matrix
