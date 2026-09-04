"""BMS protocol decoder for HinaESS Hi-5 battery.

Decodes the raw RS485 protocol found on HinaESS Hi-5 batteries.

Frame structure:
  Sync:         A5 5A (2 bytes)
  Address:      1 byte (0x01 = BMS #1, 0x02 = BMS #2, 0x03 = BMS #3, ...)
  Command:      1 byte (0x01 = data response / probe response)
  Status/Flag:  1 byte (0x00 = probe/echo, 0x14 = data frame start)
  Payload:      variable length data (data frames only)
  Checksum:     1 byte
  Terminator:   AA

Payload structure (reverse-engineered from bmsdata.bin capture):
  Block format (data frames):
    [0]         Flag byte (0x14 = BMS #1 block, 0x14/0x9B/0x9D/0xA1 = varies)
    [1-3]       3 bytes (some sequence/status info)
    [4-7]       2 × uint16 (pack voltage related)
    [8-11]      2 × uint16 (pack voltage related)
    [12]        0x10 cell-count byte indicating start of cell voltages
    [13-44]     16×2 = 32 bytes cell voltages (big-endian mV)
    [45+]       Temperature/current/status data
    [...]       ASCII strings: "BMS..." model or "10...BS..." serial

BMS #1 (addr 0x01): 1 block of 16 cells + model "BMS0000000000000SZTB"
BMS #2 (addr 0x02): 1 block of 16 cells + model "BMS0000000000000SZTB"
BMS #3 (addr 0x03): 1 block of 16 cells + serial "10101012BS00205"

Reference: https://github.com/drewzadev/ha-hinaess-powergem
"""

import logging
import struct

logger = logging.getLogger(__name__)

# ── Protocol constants ──────────────────────────────────────────────────────

SYNC = b"\xa5\x5a"
TERM = b"\xaa"
SYNC_LEN = len(SYNC)  # 2
TERM_LEN = len(TERM)  # 1

# For probe/echo frames: sync(2) + addr(1) + cmd(1) + status(1) + cs(1) + term(1)
MIN_PROBE_LEN = SYNC_LEN + 1 + 1 + 1 + 1 + TERM_LEN  # 7
# For data frames: sync(2) + addr(1) + cmd(1) + payload + cs(1) + term(1)
MIN_FRAME_LEN = MIN_PROBE_LEN  # 7 minimum

CMD_ALL = 0x01  # all frames use cmd=0x01 in the revised protocol
CMD_QUERY = 0x00  # legacy, may still appear or be used for query construction

# Probe/echo status byte value
PROBE_STATUS = 0x00
PROBE_CS = 0xFE  # Constant checksum for all probe/echo frames

# Cell voltage constants
CELLS_PER_BLOCK = 16
CELL_VOLTAGE_BYTES = 2
CELL_BLOCK_LEN = CELLS_PER_BLOCK * CELL_VOLTAGE_BYTES  # 32 bytes


# ── Helpers ──────────────────────────────────────────────────────────────────


def get_u16_be(data: bytes, offset: int) -> int:
    """Extract unsigned 16-bit big-endian integer."""
    return (data[offset] << 8) | data[offset + 1]


def is_valid_cell_voltage(mv: int) -> bool:
    """Check if a value is a plausible LiFePO4 cell voltage (in mV)."""
    return 2500 <= mv <= 4000


def get_s16_be(data: bytes, offset: int) -> int:
    """Extract signed 16-bit big-endian integer."""
    val = get_u16_be(data, offset)
    return val - 65536 if val > 32767 else val


def extract_ascii_string(
    data: bytes, offset: int, max_len: int = 32
) -> tuple[str, int]:
    """Extract an ASCII string starting at offset.

    Returns (string, next_offset). Stops at non-printable chars or max_len.
    """
    end = offset
    while end < len(data) and end - offset < max_len:
        b = data[end]
        if 0x20 <= b < 0x7F:
            end += 1
        else:
            break
    return data[offset:end].decode("ascii", errors="replace"), end


# ── Frame detection ─────────────────────────────────────────────────────────


def find_frames(data: bytes) -> list[bytes]:
    """Find all complete frames in a byte stream.

    Scans for A5 5A sync markers and extracts frames
    terminated by AA. Returns a list of raw frame bytes
    (including sync and terminator).
    """
    frames: list[bytes] = []
    i = 0
    while i < len(data):
        sync_pos = data.find(SYNC, i)
        if sync_pos == -1:
            break

        term_pos = data.find(TERM, sync_pos + SYNC_LEN)
        if term_pos == -1:
            break

        frame_end = term_pos + TERM_LEN
        frame = data[sync_pos:frame_end]
        if len(frame) >= MIN_FRAME_LEN:
            frames.append(frame)

        i = sync_pos + 1

    return frames


def validate_checksum(frame: bytes) -> bool:
    """Validate a frame's checksum.

    Checksum is at position len-2 (before the terminator AA).
    Two's complement of the sum of all bytes except checksum and terminator.

    For probe/echo frames (7 bytes total with status byte = 0x00), the
    checksum is a constant 0xFE, not computed by the sum algorithm.
    """
    if len(frame) < MIN_FRAME_LEN:
        return False

    received_checksum = frame[-2]

    # For min-length frames (sync + addr + cmd + status + cs + term),
    # the checksum is always 0xFE for all addresses
    if len(frame) == MIN_PROBE_LEN:
        return received_checksum == PROBE_CS

    # For data frames: two's complement of sum of all bytes before checksum
    checksum_bytes = frame[:-2]
    total = sum(checksum_bytes) & 0xFF
    expected = (-total) & 0xFF

    return received_checksum == expected


# ── Frame parsing ────────────────────────────────────────────────────────────


def parse_frame_header(frame: bytes) -> dict:
    """Parse the basic header of a frame.

    Returns dict with:
      - addr: int (BMS address)
      - cmd: int (command byte)
      - payload: bytes (data between header and checksum)
      - valid: bool
      - error: str (if invalid)
      - is_probe: bool (True if this is a probe/echo frame)
    """
    result: dict = {}

    if len(frame) < MIN_FRAME_LEN:
        result["valid"] = False
        result["error"] = f"Frame too short: {len(frame)} bytes"
        return result

    if frame[:SYNC_LEN] != SYNC:
        result["valid"] = False
        result["error"] = "Bad sync pattern"
        return result

    if frame[-TERM_LEN:] != TERM:
        result["valid"] = False
        result["error"] = "Bad terminator"
        return result

    addr = frame[SYNC_LEN]
    cmd = frame[SYNC_LEN + 1]

    header_end = SYNC_LEN + 2
    checksum_pos = len(frame) - 2
    payload = frame[header_end:checksum_pos]

    is_probe = (
        len(frame) == MIN_PROBE_LEN and len(payload) == 1 and payload[0] == PROBE_STATUS
    )

    result["valid"] = True
    result["addr"] = addr
    result["cmd"] = cmd
    result["payload"] = payload
    result["checksum"] = frame[checksum_pos]
    result["is_probe"] = is_probe

    return result


def find_cell_block_start(payload: bytes, offset: int) -> int:
    """Find where a cell voltage block starts in the payload.

    Scans forward from offset looking for a 0x10 byte that is
    followed by 16 consecutive valid LiFePO4 cell voltages.

    Returns the offset of the cell count byte (0x10), or -1.
    """
    i = offset
    while i + CELL_BLOCK_LEN + 1 < len(payload):
        # Look for 0x10 as potential cell count
        if payload[i] != 0x10:
            i += 1
            continue

        # Check if bytes after this look like cell voltages
        cell_start = i + 1
        valid_count = 0
        for c in range(CELLS_PER_BLOCK):
            pos = cell_start + c * CELL_VOLTAGE_BYTES
            if pos + 1 >= len(payload):
                break
            mv = get_u16_be(payload, pos)
            if is_valid_cell_voltage(mv):
                valid_count += 1
            else:
                break

        # Accept if at least 14 of 16 cells are valid LiFePO4 values
        if valid_count >= 14:
            return i

        i += 1

    return -1


def parse_cell_block(payload: bytes, cell_count_offset: int) -> dict:
    """Parse a single 16-cell voltage block starting at the cell count byte.

    Args:
        payload: The full payload bytes
        cell_count_offset: Offset of the 0x10 cell count byte

    Returns dict with cells_v list and next_offset.
    """
    result: dict = {
        "cells_v": [],
        "cell_count": 0,
    }

    cell_data_start = cell_count_offset + 1

    cells: list[float] = []
    for i in range(CELLS_PER_BLOCK):
        pos = cell_data_start + i * CELL_VOLTAGE_BYTES
        if pos + 1 >= len(payload):
            break
        mv = get_u16_be(payload, pos)
        voltage = mv / 1000.0
        cells.append(voltage)

    result["cells_v"] = cells
    result["cell_count"] = len(cells)
    result["next_offset"] = cell_data_start + CELL_BLOCK_LEN

    return result


def extract_model_info(payload: bytes) -> dict:
    """Extract model string and serial number from the payload."""
    info: dict = {}

    bms_idx = payload.find(b"BMS")
    if bms_idx >= 0:
        model_str, _ = extract_ascii_string(payload, bms_idx)
        info["model"] = model_str

    sn_idx = payload.find(b"10")
    while sn_idx >= 0:
        sn_str, _ = extract_ascii_string(payload, sn_idx)
        if "BS" in sn_str:
            info["serial"] = sn_str
            break
        sn_idx = payload.find(b"10", sn_idx + 1)

    return info


# ── Auxiliary data parsing (temperatures, status, metrics) ──────────────────


def parse_temperatures(payload: bytes) -> list[dict]:
    """Extract temperature sensor readings from the payload.

    Temperatures are stored as 6 × 0x0B-prefixed uint16 values
    starting at offset 48. The low byte appears to be temperature
    in degrees Fahrenheit.
    """
    temps: list[dict] = []
    labels = ["Battery", "MOS", "Sensor1", "Sensor2", "Sensor3", "Sensor4"]
    for i in range(6):
        pos = 48 + i * 2
        if pos + 1 >= len(payload):
            break
        if payload[pos] == 0x0B:
            deg_f = payload[pos + 1]
            deg_c = round((deg_f - 32) * 5 / 9, 1)
            label = labels[i] if i < len(labels) else f"T{i}"
            temps.append(
                {
                    "label": label,
                    "fahrenheit": deg_f,
                    "celsius": deg_c,
                }
            )
    return temps


def parse_status_metrics(payload: bytes) -> dict:
    """Extract status flags and metrics from the post-cell auxiliary data.

    Returns dict with status flags, current, and capacity estimates.
    Interpretations are tentative without captures under varying conditions.
    """
    info: dict = {}

    if len(payload) < 85:
        return info

    try:
        # Pack-voltage-related uint16 values at offsets 6-7 and 10-11
        # These appear to scale with pack voltage (higher for BMS#1, lower for BMS#3)
        uv1 = get_u16_be(payload, 6)
        uv2 = get_u16_be(payload, 10)
        info["aux_voltage_1"] = uv1  # tentative: could be voltage × 76.1
        info["aux_voltage_2"] = uv2  # tentative: could be voltage × 75.8

        # Status flags at offset 60-61
        status = get_u16_be(payload, 60)
        info["status_flags"] = status
        # Decode known bit patterns
        status_byte0 = (status >> 8) & 0xFF
        info["charging"] = bool(status_byte0 & 0x01)
        info["discharging"] = bool(status_byte0 & 0x02)

        # Current reading at offset 62-64
        # Offset 62-63 is a count/sequence-like value (6 in captures)
        # Offset 63-64 seems to be a current/temperature related field
        current_raw = get_u16_be(payload, 63)
        if current_raw > 0:
            # Current: 0x0691 = 1681 observed at idle
            # Subtract idle baseline to get signed current
            info["current_raw"] = current_raw

        # Capacity values at offset 79-82
        cap_raw_1 = get_u16_be(payload, 79)
        cap_raw_2 = get_u16_be(payload, 81)
        if cap_raw_1 > 0:
            info["capacity_raw_1"] = cap_raw_1
        if cap_raw_2 > 0:
            info["capacity_raw_2"] = cap_raw_2

        # Extra temperature sensor at offset 83-84
        if payload[83] == 0x0B:
            deg_f = payload[84]
            deg_c = round((deg_f - 32) * 5 / 9, 1)
            info["extra_temp"] = {
                "label": "Extra",
                "fahrenheit": deg_f,
                "celsius": deg_c,
            }

    except IndexError, struct.error:
        pass

    return info


# ── Battery data parsing ────────────────────────────────────────────────────


def parse_battery_data(payload: bytes, addr: int) -> dict:
    """Parse battery data from a response payload.

    Scans payload for 16-cell voltage blocks using robust start detection.
    Returns dict with all decoded parameters.
    """
    result: dict = {
        "bms_addr": addr,
        "valid": False,
    }

    if len(payload) < 15:
        result["error"] = f"Payload too short: {len(payload)} bytes"
        return result

    try:
        # Find all cell blocks
        blocks = []
        search_offset = 0

        while True:
            cell_count_offset = find_cell_block_start(payload, search_offset)
            if cell_count_offset < 0:
                break

            block = parse_cell_block(payload, cell_count_offset)
            if block["cells_v"]:
                blocks.append(block)
                search_offset = block["next_offset"]

                # Check for separator byte before next block
                if search_offset < len(payload) and payload[search_offset] == 0x01:
                    search_offset += 1

        all_cells: list[float] = []
        for block in blocks:
            all_cells.extend(block["cells_v"])

        result["cell_blocks"] = len(blocks)
        result["cells_v"] = all_cells
        result["cell_count"] = len(all_cells)

        if all_cells:
            valid_cells = [v for v in all_cells if v > 0.1]
            if valid_cells:
                min_v = min(valid_cells)
                max_v = max(valid_cells)
                min_idx = all_cells.index(min_v) + 1
                max_idx = all_cells.index(max_v) + 1
                result["min_cell_v"] = round(min_v, 3)
                result["max_cell_v"] = round(max_v, 3)
                result["min_cell_idx"] = min_idx
                result["max_cell_idx"] = max_idx
                result["cell_diff_mv"] = round((max_v - min_v) * 1000, 1)

                avg_cell = sum(valid_cells) / len(valid_cells)
                result["voltage_v"] = round(avg_cell * 16, 2)

        # Extract additional data
        temps = parse_temperatures(payload)
        if temps:
            result["temps"] = temps

        metrics = parse_status_metrics(payload)
        result.update(metrics)

        model_info = extract_model_info(payload)
        result.update(model_info)

        result["valid"] = bool(all_cells) or bool(model_info)

    except (IndexError, struct.error) as e:
        result["error"] = str(e)

    return result


def parse_response(frame: bytes) -> dict:
    """Parse a complete response frame and extract battery data.

    This is the main entry point for decoding. Takes a raw frame
    bytes (including sync and terminator) and returns a dict with
    all decoded battery parameters.
    """
    header = parse_frame_header(frame)
    if not header.get("valid"):
        result: dict = {
            "valid": False,
            "error": header.get("error", "Invalid frame header"),
        }
        return result

    # Skip probe/echo frames - they have no data payload
    if header.get("is_probe") or header["cmd"] == CMD_QUERY:
        return {
            "valid": False,
            "addr": header["addr"],
            "cmd": header["cmd"],
            "is_probe": header.get("is_probe", False),
            "error": "Probe/query frame, no data",
        }

    result = parse_battery_data(header["payload"], header["addr"])
    result["addr"] = header["addr"]
    result["cmd"] = header["cmd"]

    return result


def find_data_frames(data: bytes) -> list[dict]:
    """Convenience function: find all frames and parse data responses.

    Returns list of parsed result dicts for valid data frames.
    Skips probe/echo frames and invalid payloads.
    """
    results = []
    frames = find_frames(data)
    for frame in frames:
        result = parse_response(frame)
        if result.get("valid"):
            results.append(result)
    return results


def build_query_packet(addr: int) -> bytes:
    """Build a query packet for a given BMS address.

    Uses the revised protocol format with 2-byte sync.
    All query/probe frames use a constant checksum of 0xFE
    regardless of address, matching the actual hardware protocol.
    """
    # Frame: sync(2) + addr(1) + cmd(1) + status(1) + cs(1) + term(1)
    cmd = CMD_ALL  # 0x01
    status = PROBE_STATUS  # 0x00
    checksum = PROBE_CS  # 0xFE constant for all probe/echo frames
    return SYNC + bytes([addr, cmd, status]) + bytes([checksum]) + TERM
