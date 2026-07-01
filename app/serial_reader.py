"""Serial port reader and frame synchronizer for HinaESS BMS.

Provides a SerialPortReader that connects to /dev/ttyUSB0 (9600 8N1)
and continuously reads and decodes incoming frames. Also provides
a ReplayReader that replays a binary capture file for testing.

The reader continuously consumes serial data, synchronizes on the
A5 5A frame sync marker, extracts complete frames terminated by AA,
and silently ignores probe frames and unparseable data. Only valid
battery data frames are passed through for processing.
"""

import time
import logging
import threading
from queue import Queue
from typing import Optional, Callable

import serial

from .bms_decoder import (
    SYNC,
    TERM,
    MIN_FRAME_LEN,
    MIN_PROBE_LEN,
    find_frames,
    parse_response,
)

logger = logging.getLogger(__name__)

# Serial port defaults
BAUD_RATE = 9600
DEFAULT_PORT = "/dev/ttyUSB0"


class SerialPortReader:
    """Reads and decodes BMS frames from a physical serial port.

    Continuously consumes the RS485 serial data stream, synchronizes
    on the A5 5A frame sync pattern, extracts frames, and decodes
    them. Probe/echo frames and unparseable data are silently discarded;
    only valid battery data frames are delivered.
    """

    def __init__(
        self,
        port: str = DEFAULT_PORT,
        baudrate: int = BAUD_RATE,
        timeout: float = 1.0,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial: Optional[serial.Serial] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._frame_queue: Queue = Queue()
        self._on_frame: Optional[Callable] = None

        # Buffer for partial data between reads
        self._buffer = b""

    def connect(self) -> bool:
        """Open the serial port connection.

        Returns True on success, False on failure.
        """
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
            )
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
            logger.info("Connected to %s at %d baud", self.port, self.baudrate)
            return True
        except serial.SerialException as e:
            logger.error("Failed to open serial port %s: %s", self.port, e)
            return False

    def disconnect(self):
        """Close the serial port connection."""
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
            logger.info("Disconnected from %s", self.port)

    @property
    def is_connected(self) -> bool:
        """Check if the serial port is open."""
        return self._serial is not None and self._serial.is_open

    def start(self, on_frame: Optional[Callable] = None):
        """Start continuous background reading from the serial port.

        Data is constantly consumed from the serial port. Frames are
        detected, parsed, and only valid battery data frames are
        forwarded to the callback or placed on the internal queue.

        Args:
            on_frame: Callback function(parsed_dict) called when a
                     valid battery data frame is decoded. If None,
                     results are queued for retrieval with get_result().
        """
        if self._running:
            logger.warning("Background reader already running")
            return

        self._on_frame = on_frame
        self._running = True
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()
        logger.info("Serial reader started")

    def stop(self):
        """Stop the background reader thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        logger.info("Serial reader stopped")

    def _reader_loop(self):
        """Background thread that continuously consumes serial data.

        Reads all available data from the serial port, buffers it,
        extracts complete frames, and silently discards probe frames.
        Only valid battery data frames are forwarded.
        """
        buffer = b""
        while self._running:
            try:
                if self._serial and self._serial.in_waiting > 0:
                    chunk = self._serial.read(self._serial.in_waiting)
                    if chunk:
                        buffer += chunk

                        frames = find_frames(buffer)
                        processed_frames = []
                        for frame in frames:
                            parsed = parse_response(frame)
                            if parsed.get("valid") and parsed.get("cells_v"):
                                # Valid battery data frame
                                if self._on_frame:
                                    self._on_frame(parsed)
                                self._frame_queue.put(parsed)
                                processed_frames.append(frame)

                        # Remove processed data from buffer
                        if processed_frames:
                            last_frame = processed_frames[-1]
                            fi = buffer.find(last_frame)
                            if fi >= 0:
                                frame_end = fi + len(last_frame)
                                buffer = buffer[frame_end:]
                        elif frames:
                            # All frames were probes or invalid, remove them
                            last_frame = frames[-1]
                            fi = buffer.find(last_frame)
                            if fi >= 0:
                                frame_end = fi + len(last_frame)
                                buffer = buffer[frame_end:]
                        elif len(buffer) > 4096:
                            # No frames found, trim buffer to avoid unbounded growth
                            sync_pos = buffer.find(SYNC)
                            if sync_pos > 0:
                                buffer = buffer[sync_pos:]
                            elif sync_pos == -1:
                                # No sync pattern at all, keep last 32 bytes
                                buffer = buffer[-32:]
            except serial.SerialException:
                break
            except Exception:
                logger.exception("Error in reader loop")
            time.sleep(0.01)

    def get_result(self, timeout: float = 1.0) -> Optional[dict]:
        """Get a parsed battery data result from the queue.

        Args:
            timeout: Maximum time to wait in seconds.

        Returns:
            Parsed result dict, or None if timeout expires.
        """
        try:
            return self._frame_queue.get(timeout=timeout)
        except Exception:
            return None


class ReplayReader:
    """Replays a binary capture file as simulated serial input.

    Used for testing the decoder without physical hardware.
    The replay can either return pre-parsed frames or simulate
    byte-by-byte serial reads.
    """

    def __init__(self, capture_path: str):
        self.capture_path = capture_path
        self._data: bytes = b""
        self._frames: list[bytes] = []
        self._pos = 0
        self._frame_idx = 0
        self._in_waiting = 0
        self._buffer = b""

        self._load_capture()

    def _load_capture(self):
        """Load the binary capture file and extract frames."""
        with open(self.capture_path, "rb") as f:
            self._data = f.read()
        self._frames = find_frames(self._data)
        logger.info(
            "Loaded %s: %d bytes, %d frames",
            self.capture_path,
            len(self._data),
            len(self._frames),
        )

    @property
    def frames(self) -> list[bytes]:
        """Get all extracted frames."""
        return self._frames

    @property
    def data(self) -> bytes:
        """Get the raw capture data."""
        return self._data

    # Simulation methods to mimic pyserial.Serial
    @property
    def in_waiting(self) -> int:
        """Simulate serial in_waiting by returning bytes available."""
        return self._in_waiting

    def read(self, size: int = 1) -> bytes:
        """Simulate serial read by returning data from buffer."""
        available = min(size, len(self._buffer))
        chunk = self._buffer[:available]
        self._buffer = self._buffer[available:]
        self._in_waiting = len(self._buffer)
        return chunk

    def write(self, data: bytes):
        """Simulate serial write (no-op, but logs)."""
        logger.debug("TX > %s", data.hex())

    def flush(self):
        """Simulate serial flush (no-op)."""
        pass

    def reset_input_buffer(self):
        """Reset the simulated input buffer."""
        self._buffer = b""
        self._in_waiting = 0

    def reset_output_buffer(self):
        """Reset simulated output buffer (no-op)."""
        pass

    def simulate_frame_injection(self, frame_index: int = -1) -> bytes:
        """Inject a frame into the read buffer to simulate receiving it.

        Args:
            frame_index: Index of the frame to inject. -1 for next frame.

        Returns:
            The injected frame bytes.
        """
        if frame_index >= 0:
            self._frame_idx = frame_index

        if self._frame_idx >= len(self._frames):
            return b""

        frame = self._frames[self._frame_idx]
        self._buffer += frame
        self._in_waiting = len(self._buffer)
        self._frame_idx += 1
        return frame

    def close(self):
        """Clean up (no-op for replay)."""
        pass

    @property
    def is_open(self) -> bool:
        """Replay reader is always open."""
        return True

    @property
    def frame_count(self) -> int:
        """Number of available frames."""
        return len(self._frames)

    def read_all_frames(self) -> list[dict]:
        """Parse all frames in the capture and return decoded results.

        Filters out probe/echo frames and returns only valid battery
        data responses with cell voltages.
        """
        results = []
        for frame in self._frames:
            result = parse_response(frame)
            if result.get("valid") and result.get("cells_v"):
                results.append(result)
        return results
