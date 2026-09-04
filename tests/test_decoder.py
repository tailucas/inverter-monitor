"""Unit tests for the HinaESS BMS protocol decoder.

These tests use the bmsdata.bin and bmsdata2.bin capture files as
simulated input, allowing testing without physical serial hardware.
Tests run against each capture file to verify the decoder works
regardless of the starting position in the serial data stream.
"""

import os

import pytest

# Project root for finding test data
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTURE_PATHS = {
    "bmsdata1.bin": os.path.join(PROJECT_ROOT, "tests", "bmsdata1.bin"),
    "bmsdata2.bin": os.path.join(PROJECT_ROOT, "tests", "bmsdata2.bin"),
}


@pytest.fixture(params=["bmsdata1.bin", "bmsdata2.bin"])
def capture_name(request):
    """Parameterized fixture: returns the capture filename."""
    return request.param


@pytest.fixture
def capture_data(capture_name):
    """Load a binary capture file."""
    path = CAPTURE_PATHS[capture_name]
    with open(path, "rb") as f:
        return f.read()


@pytest.fixture
def decoder():
    """Import and return the decoder module."""
    import sys

    sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
    from app import bms_decoder as decoder

    return decoder


# ── Basic frame detection tests ─────────────────────────────────────────────


class TestFrameDetection:
    def test_find_frames_returns_list(self, capture_data, decoder):
        """verify frames are extracted from the capture"""
        frames = decoder.find_frames(capture_data)
        assert isinstance(frames, list)
        assert len(frames) > 0

    def test_find_frames_count(self, capture_data, decoder):
        """verify correct number of frames are found"""
        frames = decoder.find_frames(capture_data)
        # Both captures have at least 60 frames
        assert len(frames) >= 60

    def test_each_frame_has_minimum_length(self, capture_data, decoder):
        """verify all frames meet minimum length requirement"""
        frames = decoder.find_frames(capture_data)
        for frame in frames:
            assert len(frame) >= decoder.MIN_FRAME_LEN

    def test_each_frame_starts_with_sync(self, capture_data, decoder):
        """verify all frames start with sync pattern"""
        frames = decoder.find_frames(capture_data)
        for frame in frames:
            assert frame[:2] == decoder.SYNC

    def test_each_frame_ends_with_terminator(self, capture_data, decoder):
        """verify all frames end with AA terminator"""
        frames = decoder.find_frames(capture_data)
        for frame in frames:
            assert frame[-1:] == decoder.TERM


# ── Frame header parsing tests ──────────────────────────────────────────────


class TestFrameHeader:
    def test_parse_probe_frame(self, capture_data, decoder):
        """verify probe frames (cmd=0x01 with status 0x00) are identified"""
        frames = decoder.find_frames(capture_data)

        # Find a probe/echo frame (7 bytes, status byte = 0x00)
        probe_frames = [f for f in frames if len(f) == decoder.MIN_PROBE_LEN]
        assert len(probe_frames) > 0, "No probe frames found in capture"

        for frame in probe_frames:
            header = decoder.parse_frame_header(frame)
            assert header["valid"]
            assert header["is_probe"]
            assert header["cmd"] == decoder.CMD_ALL
            assert 0x01 <= header["addr"] <= 0x1F

    def test_parse_data_frame_bms1(self, capture_data, decoder):
        """verify BMS #1 data frame is parsed correctly"""
        frames = decoder.find_frames(capture_data)

        for frame in frames:
            hdr = decoder.parse_frame_header(frame)
            if (
                hdr["valid"]
                and hdr["addr"] == 0x01
                and not hdr.get("is_probe")
                and len(hdr["payload"]) > 40
            ):
                assert hdr["addr"] == 0x01
                assert hdr["cmd"] == decoder.CMD_ALL
                return

        pytest.fail("No BMS #1 data frame found")

    def test_parse_data_frame_bms2(self, capture_data, decoder):
        """verify BMS #2 data frame is parsed correctly"""
        frames = decoder.find_frames(capture_data)

        for frame in frames:
            hdr = decoder.parse_frame_header(frame)
            if (
                hdr["valid"]
                and hdr["addr"] == 0x02
                and not hdr.get("is_probe")
                and len(hdr["payload"]) > 40
            ):
                assert hdr["addr"] == 0x02
                assert hdr["cmd"] == decoder.CMD_ALL
                return

        pytest.fail("No BMS #2 data frame found")

    def test_parse_data_frame_bms3(self, capture_data, decoder):
        """verify BMS #3 data frame is parsed correctly"""
        frames = decoder.find_frames(capture_data)

        for frame in frames:
            hdr = decoder.parse_frame_header(frame)
            if (
                hdr["valid"]
                and hdr["addr"] == 0x03
                and not hdr.get("is_probe")
                and len(hdr["payload"]) > 40
            ):
                assert hdr["addr"] == 0x03
                assert hdr["cmd"] == decoder.CMD_ALL
                return

        pytest.fail("No BMS #3 data frame found")

    def test_short_frame_invalid(self, decoder):
        """verify too-short frames are rejected"""
        short_frame = b"\xa5\x5a\x01\x01"
        result = decoder.parse_frame_header(short_frame)
        assert not result.get("valid", False)

    def test_bad_sync_invalid(self, decoder):
        """verify frames with bad sync are rejected"""
        bad_frame = b"\x00\x00\x01\x01\x00\xfe\xaa"
        result = decoder.parse_frame_header(bad_frame)
        assert not result.get("valid", False)


# ── Cell voltage parsing tests ──────────────────────────────────────────────


class TestCellVoltageParsing:
    def _check_cell_results(self, results, addr, expected_cells, expected_blocks):
        """Helper to verify cell results for a given BMS address."""
        addr_results = [r for r in results if r.get("addr") == addr]
        assert len(addr_results) > 0, f"No valid results for addr=0x{addr:02x}"
        for result in addr_results:
            assert result["cell_count"] == expected_cells, (
                f"BMS 0x{addr:02x} expected {expected_cells} "
                f"cells, got {result['cell_count']}",
            )
            assert len(result["cells_v"]) == expected_cells
            assert result["cell_blocks"] == expected_blocks

    def test_bms1_has_16_cells(self, capture_data, decoder):
        """verify BMS #1 reports 16 cells"""
        results = decoder.find_data_frames(capture_data)
        self._check_cell_results(results, 0x01, 16, 1)

    def test_bms2_has_16_cells(self, capture_data, decoder):
        """verify BMS #2 reports 16 cells (was 32 in old protocol)"""
        results = decoder.find_data_frames(capture_data)
        self._check_cell_results(results, 0x02, 16, 1)

    def test_bms3_has_16_cells(self, capture_data, decoder):
        """verify BMS #3 reports 16 cells"""
        results = decoder.find_data_frames(capture_data)
        self._check_cell_results(results, 0x03, 16, 1)

    def test_cell_voltages_in_lifepo4_range(self, capture_data, decoder):
        """verify cell voltages are in plausible LiFePO4 range (3.0-3.4V)"""
        results = decoder.find_data_frames(capture_data)

        for result in results:
            for v in result["cells_v"]:
                if v > 0.1:  # skip placeholder zero values
                    assert 2.5 <= v <= 4.0, f"Cell voltage {v}V out of LiFePO4 range"

    def test_cell_voltage_consistency(self, capture_data, decoder):
        """verify all cells in a given frame are within reasonable balance"""
        results = decoder.find_data_frames(capture_data)

        for result in results:
            cells = [v for v in result["cells_v"] if v > 0.1]
            if len(cells) >= 2:
                max_diff = max(cells) - min(cells)
                # Well-balanced LiFePO4 cells should differ by < 50mV
                assert max_diff < 0.050, (
                    f"Cell voltage diff {max_diff * 1000:.1f}mV exceeds 50mV"
                )

    def test_min_max_cell_tracking(self, capture_data, decoder):
        """verify min/max cell voltage tracking is correct"""
        results = decoder.find_data_frames(capture_data)

        for result in results:
            cells = result["cells_v"]
            valid = [v for v in cells if v > 0.1]
            if valid:
                min_v = min(valid)
                max_v = max(valid)
                assert result["min_cell_v"] == round(min_v, 3)
                assert result["max_cell_v"] == round(max_v, 3)
                assert result["cell_diff_mv"] == round((max_v - min_v) * 1000, 1)

    def test_pack_voltage_reasonable(self, capture_data, decoder):
        """verify estimated pack voltage is reasonable for a 16S LiFePO4"""
        results = decoder.find_data_frames(capture_data)

        for result in results:
            if "voltage_v" in result:
                # 16S LiFePO4: nominal 51.2V, typical range 48-58V
                assert 48.0 <= result["voltage_v"] <= 58.0, (
                    f"Pack voltage {result['voltage_v']}V out of range"
                )


# ── Model/serial string parsing tests ────────────────────────────────────────


class TestModelSerial:
    def test_bms1_has_model_string(self, capture_data, decoder):
        """verify BMS #1 frames contain model string"""
        results = decoder.find_data_frames(capture_data)
        bms1_results = [r for r in results if r.get("addr") == 0x01]

        assert len(bms1_results) > 0
        for result in bms1_results:
            assert "model" in result, f"BMS #1 missing model: {result}"
            assert "BMS" in result["model"], (
                f"BMS #1 model doesn't contain 'BMS': {result['model']}"
            )

    def test_bms2_has_model_string(self, capture_data, decoder):
        """verify BMS #2 frames contain model string (was serial in old protocol)"""
        results = decoder.find_data_frames(capture_data)
        bms2_results = [r for r in results if r.get("addr") == 0x02]

        assert len(bms2_results) > 0
        for result in bms2_results:
            assert "model" in result, f"BMS #2 missing model: {result}"
            assert "BMS" in result["model"], (
                f"BMS #2 model doesn't contain 'BMS': {result['model']}"
            )

    def test_bms3_has_serial_string(self, capture_data, decoder):
        """verify BMS #3 frames contain serial number"""
        results = decoder.find_data_frames(capture_data)
        bms3_results = [r for r in results if r.get("addr") == 0x03]

        assert len(bms3_results) > 0
        for result in bms3_results:
            assert "serial" in result, f"BMS #3 missing serial: {result}"
            assert "BS" in result["serial"], (
                f"BMS #3 serial doesn't contain 'BS': {result['serial']}"
            )

    def test_bms3_serial_format(self, capture_data, decoder):
        """verify BMS #3 serial number matches expected format"""
        results = decoder.find_data_frames(capture_data)
        bms3_results = [r for r in results if r.get("addr") == 0x03]

        for result in bms3_results:
            serial = result["serial"]
            # Expected format: 10XX10XX12BS00205
            assert len(serial) >= 10
            assert "BS" in serial


# ── Multi-block frame parsing tests ─────────────────────────────────────────


class TestMultiBlock:
    def test_bms1_is_single_block(self, capture_data, decoder):
        """verify BMS #1 frames contain exactly 1 cell block"""
        results = decoder.find_data_frames(capture_data)
        bms1_results = [r for r in results if r.get("addr") == 0x01]

        for result in bms1_results:
            assert result["cell_blocks"] == 1, (
                f"BMS #1 expected 1 block, got {result['cell_blocks']}"
            )

    def test_bms2_is_single_block(self, capture_data, decoder):
        """verify BMS #2 frames contain exactly 1 cell block (was 2 in old protocol)"""
        results = decoder.find_data_frames(capture_data)
        bms2_results = [r for r in results if r.get("addr") == 0x02]

        for result in bms2_results:
            assert result["cell_blocks"] == 1, (
                f"BMS #2 expected 1 block, got {result['cell_blocks']}"
            )

    def test_bms3_is_single_block(self, capture_data, decoder):
        """verify BMS #3 frames contain exactly 1 cell block"""
        results = decoder.find_data_frames(capture_data)
        bms3_results = [r for r in results if r.get("addr") == 0x03]

        for result in bms3_results:
            assert result["cell_blocks"] == 1, (
                f"BMS #3 expected 1 block, got {result['cell_blocks']}"
            )


# ── Replay reader tests ─────────────────────────────────────────────────────


class TestReplayReader:
    def test_replay_reader_loads_capture(self, capture_name, decoder):
        """verify ReplayReader loads and parses a capture"""
        import sys

        sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
        from app.serial_reader import ReplayReader

        path = CAPTURE_PATHS[capture_name]
        reader = ReplayReader(path)
        assert reader.frame_count > 0
        assert len(reader.frames) > 0

    def test_replay_reader_frame_injection(self, capture_name, decoder):
        """verify frame injection works for a capture"""
        import sys

        sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
        from app.serial_reader import ReplayReader

        path = CAPTURE_PATHS[capture_name]
        reader = ReplayReader(path)
        # Find and inject a BMS #1 data frame (first long frame)
        for i, f in enumerate(reader.frames):
            if len(f) > 10 and f[2] == 0x01:
                frame = reader.simulate_frame_injection(i)
                break
        else:
            frame = reader.simulate_frame_injection(0)

        assert len(frame) >= decoder.MIN_FRAME_LEN
        assert frame[:2] == decoder.SYNC

    def test_replay_read_all_frames(self, capture_name, decoder):
        """verify read_all_frames returns parsed results for a capture"""
        import sys

        sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
        from app.serial_reader import ReplayReader

        path = CAPTURE_PATHS[capture_name]
        reader = ReplayReader(path)
        results = reader.read_all_frames()
        data_results = [r for r in results if r.get("valid") and r.get("cells_v")]
        assert len(data_results) >= 4  # should have multiple BMS #1, #2 and #3 frames


# ── Known content extraction tests ──────────────────────────────────────────


class TestKnownContent:
    """Tests that verify known data extraction works on both capture files."""

    def test_known_model_strings(self, capture_data, decoder):
        """verify both BMS #1 and #2 have the expected model string"""
        results = decoder.find_data_frames(capture_data)

        # Check BMS #1
        bms1 = [r for r in results if r.get("addr") == 0x01 and r.get("model")]
        assert len(bms1) > 0
        for r in bms1:
            assert r["model"] == "BMS0000000000000SZTB", (
                f"BMS #1 model mismatch: {r['model']}"
            )

        # Check BMS #2
        bms2 = [r for r in results if r.get("addr") == 0x02 and r.get("model")]
        assert len(bms2) > 0
        for r in bms2:
            assert r["model"] == "BMS0000000000000SZTB", (
                f"BMS #2 model mismatch: {r['model']}"
            )

    def test_known_serial_number(self, capture_data, decoder):
        """verify BMS #3 has the expected serial number"""
        results = decoder.find_data_frames(capture_data)

        bms3 = [r for r in results if r.get("addr") == 0x03 and r.get("serial")]
        assert len(bms3) > 0
        for r in bms3:
            assert r["serial"] == "10101012BS00205", (
                f"BMS #3 serial mismatch: {r['serial']}"
            )

    def test_known_cell_count(self, capture_data, decoder):
        """verify all BMS units have exactly 16 cells"""
        results = decoder.find_data_frames(capture_data)

        for r in results:
            assert r["cell_count"] == 16, (
                f"BMS 0x{r['addr']:02x} has {r['cell_count']} cells, expected 16"
            )
            assert len(r["cells_v"]) == 16

    def test_known_cell_blocks(self, capture_data, decoder):
        """verify all BMS units have exactly 1 cell block"""
        results = decoder.find_data_frames(capture_data)

        for r in results:
            assert r["cell_blocks"] == 1, (
                f"BMS 0x{r['addr']:02x} has {r['cell_blocks']} blocks, expected 1"
            )

    def test_known_pack_voltage_variation(self, capture_data, decoder):
        """verify pack voltage is in the expected 52-53V range"""
        results = decoder.find_data_frames(capture_data)

        for r in results:
            v = r.get("voltage_v")
            if v:
                assert 52.0 <= v <= 53.0, (
                    f"BMS 0x{r['addr']:02x} pack voltage {v}V "
                    f"outside expected 52-53V range",
                )

    def test_known_addresses_present(self, capture_data, decoder):
        """verify all three expected BMS addresses (0x01, 0x02, 0x03) are present"""
        results = decoder.find_data_frames(capture_data)

        addrs_found = set(r["addr"] for r in results)
        for expected_addr in [0x01, 0x02, 0x03]:
            assert expected_addr in addrs_found, (
                f"BMS 0x{expected_addr:02x} not found in decoded data"
            )

    def test_frame_count_consistency(self, capture_data, decoder):
        """verify the ratio of probe to data frames is consistent"""
        frames = decoder.find_frames(capture_data)
        probe_count = sum(1 for f in frames if len(f) == decoder.MIN_PROBE_LEN)
        data_count = sum(1 for f in frames if len(f) > decoder.MIN_PROBE_LEN)

        assert data_count > 0
        assert probe_count > data_count * 8  # many probes per data frame

    def test_no_corrupt_frames(self, capture_data, decoder):
        """verify no frames have invalid checksums for probe/query frames"""
        frames = decoder.find_frames(capture_data)
        probe_frames = [f for f in frames if len(f) == decoder.MIN_PROBE_LEN]
        for frame in probe_frames:
            assert decoder.validate_checksum(frame), (
                f"Probe frame has invalid checksum: {frame.hex()}"
            )


# ── Build query packet tests ────────────────────────────────────────────────


class TestQueryPacket:
    def test_build_query_bms1(self, decoder):
        """verify query packet for BMS #1 is correctly formatted"""
        packet = decoder.build_query_packet(0x01)
        assert packet[:2] == decoder.SYNC
        assert packet[2] == 0x01  # addr
        assert packet[3] == decoder.CMD_ALL  # cmd
        assert packet[4] == decoder.PROBE_STATUS
        assert packet[-1:] == decoder.TERM

    def test_build_query_bms2(self, decoder):
        """verify query packet for BMS #2 is correctly formatted"""
        packet = decoder.build_query_packet(0x02)
        assert packet[:2] == decoder.SYNC
        assert packet[2] == 0x02
        assert packet[3] == decoder.CMD_ALL
        assert packet[4] == decoder.PROBE_STATUS

    def test_build_query_bms3(self, decoder):
        """verify query packet for BMS #3 is correctly formatted"""
        packet = decoder.build_query_packet(0x03)
        assert packet[:2] == decoder.SYNC
        assert packet[2] == 0x03
        assert packet[3] == decoder.CMD_ALL
        assert packet[4] == decoder.PROBE_STATUS

    def test_query_packet_min_length(self, decoder):
        """verify query packets meet minimum frame length"""
        for addr in [0x01, 0x02, 0x03]:
            packet = decoder.build_query_packet(addr)
            assert len(packet) >= decoder.MIN_FRAME_LEN

    def test_query_packet_checksum(self, decoder):
        """verify query packet checksum is valid"""
        for addr in [0x01, 0x02, 0x03]:
            packet = decoder.build_query_packet(addr)
            assert decoder.validate_checksum(packet), (
                f"Query packet for addr 0x{addr:02x} has invalid checksum"
            )

    def test_query_packet_format_matches_probe(self, decoder):
        """verify query packet format matches probe/echo frame format"""
        for addr in [0x01, 0x02, 0x03]:
            packet = decoder.build_query_packet(addr)
            assert len(packet) == decoder.MIN_PROBE_LEN
            # All query packets should have constant checksum 0xFE
            assert packet[-2] == decoder.PROBE_CS


# ── Edge case tests ─────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_data_no_frames(self, decoder):
        """verify empty data produces no frames"""
        frames = decoder.find_frames(b"")
        assert len(frames) == 0

    def test_noise_data_no_frames(self, decoder):
        """verify random data produces no frames"""
        noise = b"\xff" * 100 + b"\x00" * 100 + b"\xaa" * 100
        frames = decoder.find_frames(noise)
        assert len(frames) == 0

    def test_partial_frame_at_end(self, decoder):
        """verify partial sync at end of data doesn't crash"""
        partial = b"\xa5\x5a\x01\x01\x00"  # missing terminator and checksum
        frames = decoder.find_frames(partial)
        assert len(frames) == 0  # should not find incomplete frames

    def test_multiple_frames_in_stream(self, capture_data, decoder):
        """verify that repeating the capture multiple times works"""
        multi = capture_data * 3
        frames = decoder.find_frames(multi)
        # Should have roughly 3x the frames
        single_count = len(decoder.find_frames(capture_data))
        # Allow for boundary artifacts
        assert len(frames) >= single_count * 2

    def test_extract_ascii_string_basic(self, decoder):
        """verify basic ASCII string extraction"""
        data = b"Hello\x00World"
        s, next_pos = decoder.extract_ascii_string(data, 0)
        assert s == "Hello"
        assert next_pos == 5

    def test_extract_ascii_string_printable_only(self, decoder):
        """verify extraction stops at non-printable chars"""
        data = b"ABC\xffDEF"
        s, _ = decoder.extract_ascii_string(data, 0)
        assert s == "ABC"

    def test_is_valid_cell_voltage(self, decoder):
        """verify cell voltage validation"""
        assert decoder.is_valid_cell_voltage(3300)  # 3.3V - typical
        assert decoder.is_valid_cell_voltage(2700)  # 2.7V - low but valid
        assert decoder.is_valid_cell_voltage(3650)  # 3.65V - max for LiFePO4
        assert not decoder.is_valid_cell_voltage(1000)  # too low
        assert not decoder.is_valid_cell_voltage(5000)  # too high
        assert not decoder.is_valid_cell_voltage(0)  # zero

    def test_get_u16_be(self, decoder):
        """verify big-endian uint16 extraction"""
        data = b"\x0c\xa0"
        assert decoder.get_u16_be(data, 0) == 0x0CA0  # 3232

    def test_get_s16_be_positive(self, decoder):
        """verify signed 16-bit extraction for positive values"""
        data = b"\x00\x50"
        assert decoder.get_s16_be(data, 0) == 80

    def test_get_s16_be_negative(self, decoder):
        """verify signed 16-bit extraction for negative values"""
        data = b"\xff\x38"  # -200 in two's complement
        assert decoder.get_s16_be(data, 0) == -200
