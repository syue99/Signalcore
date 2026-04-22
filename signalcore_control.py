"""
signalcore_control.py
---------------------
Minimal Python control for SignalCore SC5507A / SC5508A signal generators
over USB, using ctypes to call the vendor-supplied 64-bit Windows DLL
(sc5507n8a_psg.dll).

Functions exposed:
    turn_on(serial_number)                   -> enable RF output
    turn_off(serial_number)                  -> disable RF output
    set_frequency(serial_number, hz)         -> set output frequency in Hz
                                                (blocked while in listen mode)
    start_listening_mode(serial_number,
                         frequencies_hz,
                         trigger_edge="rising",
                         power_dbm=None)     -> load a frequency list and arm
                                                the hardware trigger; each TTL
                                                edge advances one step, wraps
                                                to the first after the last
    stop_listening_mode(serial_number)       -> return to single-tone mode

Requirements:
    - 64-bit Python on Windows
    - SignalCore driver installed so that sc5507n8a_psg.dll is on disk
    - libusb-1.0.dll reachable on the DLL search path (SignalCore's
      installer normally handles this)
    - winusb.sys bound to the device (handled by the installer)

Function signatures below are confirmed against:
    - `dumpbin /exports sc5507n8a_psg.dll` (for exact exported symbol names)
    - SignalCore's shipped example.cpp / example_mod.cpp (for argument counts)
    - SignalCore's shipped header files sci_br_psg_defs.h and
      sc5507n8a_psg_functions.h (for struct layouts and prototypes)

Note: the vendor's example.cpp calls `SetOutputEnable(handle, 0, 1)` -- that
is, the function takes THREE arguments (handle, pulse_mode, output_enable),
not two as the manual text suggests. The first extra arg controls pulse
modulation capability; we pass 0 (disabled) for straight CW operation.
"""

import ctypes
from ctypes import (
    c_char_p,
    c_double,
    c_float,
    c_int,
    c_uint8,
    c_uint32,
    c_void_p,
    byref,
    POINTER,
    Structure,
)
import os

# -----------------------------------------------------------------------------
# Configuration: update this path to wherever the SignalCore installer put the
# 64-bit DLL on your machine.
# -----------------------------------------------------------------------------
_DLL_PATH = r"C:\Program Files\SignalCore\SC5507_8A\api\c\x64\sc5507n8a_psg.dll"

# -----------------------------------------------------------------------------
# Constants (from the manual Appendix 7.1 and the shipped headers)
# -----------------------------------------------------------------------------
SCI_SUCCESS = 0

# sci_comm_interface_t enum
PCI_INT = 0
USB_INT = 1
RS232_INT = 2

# RF frequency limits.  The SC5508A generates DC to 6.25 GHz.  Separately,
# the DLL's list-mode buffer documentation (ListBufferWrite header comment)
# states the buffer accepts 100 MHz to 20 GHz, so for list-mode entries we
# enforce a tighter lower bound than for plain CW.
_MAX_FREQ_HZ = 6.25e9
_LIST_MIN_FREQ_HZ = 100e6

# Maximum list-mode buffer size per ListBufferPoints header comment.
_LIST_MAX_POINTS = 2048

# list_mode_t byte pattern for "TTL steps the list, wrap to first, no trig-out".
# Fields (see sci_br_psg_defs.h):
#   sweep_mode=0        -> use buffer list, not start-stop-step
#   sweep_dir=0         -> forward
#   tri_waveform=0      -> sawtooth (irrelevant for step_on_hw_trig=1)
#   hw_trigger=1        -> use hardware trigger, not soft
#   step_on_hw_trig=1   -> each trigger advances ONE step (vs. whole sweep)
#   return_to_start=1   -> wrap to first freq after end of cycle
#   trig_out_enable=0   -> do not emit a trigger-out pulse
#   trig_out_on_cycle=0 -> (irrelevant when trig_out_enable=0)
_LIST_MODE_STEP_WRAP = (0, 0, 0, 1, 1, 1, 0, 0)


# -----------------------------------------------------------------------------
# ctypes Structure mirrors of the vendor's C structs.  Field names and order
# follow sci_br_psg_defs.h exactly -- do not rearrange.
# -----------------------------------------------------------------------------
class list_mode_t(Structure):
    _fields_ = [
        ("sweep_mode", c_uint8),
        ("sweep_dir", c_uint8),
        ("tri_waveform", c_uint8),
        ("hw_trigger", c_uint8),
        ("step_on_hw_trig", c_uint8),
        ("return_to_start", c_uint8),
        ("trig_out_enable", c_uint8),
        ("trig_out_on_cycle", c_uint8),
    ]


class hw_trigger_t(Structure):
    _fields_ = [
        ("edge", c_uint8),          # 0 = falling, 1 = rising
        ("pxi_enable", c_uint8),    # 0 for USB (no PXI backplane)
        ("pxi_line", c_uint8),      # 0-7, ignored when pxi_enable=0
    ]


class pll_status_t(Structure):
    _fields_ = [
        ("sum_pll_ld", c_uint8),
        ("crs_pll_ld", c_uint8),
        ("fine_pll_ld", c_uint8),
        ("crs_ref_pll_ld", c_uint8),
        ("crs_frac_pll_ld", c_uint8),
        ("ref_100_pll_ld", c_uint8),
        ("ref_10_pll_ld", c_uint8),
        ("sum_vco_pll_ld", c_uint8),
    ]


class operate_status_t(Structure):
    _fields_ = [
        ("lock_mode", c_uint8),
        ("loop_gain", c_uint8),
        ("harmonic_ss", c_uint8),
        ("force_low_path", c_uint8),
        ("cont_dc_phase", c_uint8),
        ("ext_ref_lock_enable", c_uint8),
        ("ref_out_select", c_uint8),
        ("pxi_clk_enable", c_uint8),
        ("output_enable", c_uint8),
        ("alc_mode", c_uint8),
        ("auto_pwr_disable", c_uint8),
        ("pulse_mode", c_uint8),
        ("synth_standby", c_uint8),
        ("sensor_enable", c_uint8),
        ("sensor_mode", c_uint8),
        ("rf_mode", c_uint8),          # 0 = single-tone, 1 = list/sweep
        ("list_mode_running", c_uint8),
        ("ext_ref_detect", c_uint8),
        ("over_temp", c_uint8),
    ]


class device_status_t(Structure):
    _fields_ = [
        ("pll_status", pll_status_t),
        ("operate_status", operate_status_t),
        ("list_mode", list_mode_t),
    ]


class device_rf_params_t(Structure):
    _fields_ = [
        ("frequency", c_double),
        ("sweep_start_freq", c_double),
        ("sweep_stop_freq", c_double),
        ("sweep_step_freq", c_double),
        ("sweep_dwell_time", c_uint32),
        ("sweep_cycles", c_uint32),
        ("buffer_points", c_uint32),
        ("rf_phase_offset", c_float),
        ("power_level", c_float),
        ("sensor_frequency", c_double),
    ]


# -----------------------------------------------------------------------------
# DLL loading. Done lazily so importing this module doesn't blow up on a
# machine without the driver installed.
# -----------------------------------------------------------------------------
_dll = None


def _load_dll() -> ctypes.CDLL:
    """Load the SignalCore DLL on first use, bind signatures, and cache."""
    global _dll
    if _dll is not None:
        return _dll

    if not os.path.exists(_DLL_PATH):
        raise FileNotFoundError(
            "Could not find SignalCore DLL at:\n  {}\n"
            "Edit _DLL_PATH at the top of signalcore_control.py to point at "
            "your installed sc5507n8a_psg.dll (64-bit).".format(_DLL_PATH)
        )

    # All exported functions are __cdecl per the manual, so use CDLL.
    dll = ctypes.CDLL(_DLL_PATH)

    # --- sc5507n8a_psgOpenDevice ---
    dll.sc5507n8a_psgOpenDevice.argtypes = [
        c_int,               # commInterface
        c_char_p,            # id (serial number string)
        c_uint8,             # baudrate (ignored on USB)
        POINTER(c_void_p),   # deviceHandle (out)
    ]
    dll.sc5507n8a_psgOpenDevice.restype = c_int

    # --- sc5507n8a_psgCloseDevice ---
    dll.sc5507n8a_psgCloseDevice.argtypes = [c_void_p]
    dll.sc5507n8a_psgCloseDevice.restype = c_int

    # --- sc5507n8a_psgSetFrequency ---
    dll.sc5507n8a_psgSetFrequency.argtypes = [c_void_p, c_double]
    dll.sc5507n8a_psgSetFrequency.restype = c_int

    # --- sc5507n8a_psgSetOutputEnable ---
    # (handle, pulse_mode, output_enable) -- 3 args per example.cpp.
    dll.sc5507n8a_psgSetOutputEnable.argtypes = [c_void_p, c_uint8, c_uint8]
    dll.sc5507n8a_psgSetOutputEnable.restype = c_int

    # --- sc5507n8a_psgSetRfMode ---
    # 0 = fixed single tone, 1 = list/sweep mode
    dll.sc5507n8a_psgSetRfMode.argtypes = [c_void_p, c_uint8]
    dll.sc5507n8a_psgSetRfMode.restype = c_int

    # --- sc5507n8a_psgListModeConfig ---
    dll.sc5507n8a_psgListModeConfig.argtypes = [c_void_p, POINTER(list_mode_t)]
    dll.sc5507n8a_psgListModeConfig.restype = c_int

    # --- sc5507n8a_psgListHwTrigConfig ---
    dll.sc5507n8a_psgListHwTrigConfig.argtypes = [c_void_p, POINTER(hw_trigger_t)]
    dll.sc5507n8a_psgListHwTrigConfig.restype = c_int

    # --- sc5507n8a_psgListBufferWrite ---
    # (handle, double* freqs_hz, float* levels_dbm, int n)
    dll.sc5507n8a_psgListBufferWrite.argtypes = [
        c_void_p,
        POINTER(c_double),
        POINTER(c_float),
        c_int,
    ]
    dll.sc5507n8a_psgListBufferWrite.restype = c_int

    # --- sc5507n8a_psgListCycleCount ---
    # 0 = cycle continuously.
    dll.sc5507n8a_psgListCycleCount.argtypes = [c_void_p, c_uint32]
    dll.sc5507n8a_psgListCycleCount.restype = c_int

    # --- sc5507n8a_psgSweepDwellTime ---
    # Units of 500 us per tick.
    dll.sc5507n8a_psgSweepDwellTime.argtypes = [c_void_p, c_uint32]
    dll.sc5507n8a_psgSweepDwellTime.restype = c_int

    # --- sc5507n8a_psgFetchDeviceStatus ---
    dll.sc5507n8a_psgFetchDeviceStatus.argtypes = [
        c_void_p,
        POINTER(device_status_t),
    ]
    dll.sc5507n8a_psgFetchDeviceStatus.restype = c_int

    # --- sc5507n8a_psgFetchRfParameters ---
    dll.sc5507n8a_psgFetchRfParameters.argtypes = [
        c_void_p,
        POINTER(device_rf_params_t),
    ]
    dll.sc5507n8a_psgFetchRfParameters.restype = c_int

    _dll = dll
    return _dll


def _check(name: str, status: int) -> None:
    """Raise if a SignalCore call returned non-success."""
    if status != SCI_SUCCESS:
        raise RuntimeError(
            "SignalCore {!r} failed with SCISTATUS={}".format(name, status)
        )


# -----------------------------------------------------------------------------
# Connection context manager. Every public call opens, acts, closes.
# -----------------------------------------------------------------------------
class _Connection:
    """Open a USB connection by serial number; guarantee close on exit."""

    def __init__(self, serial_number: str) -> None:
        self.serial_number = serial_number
        self._dll = _load_dll()
        self._handle = c_void_p()

    def __enter__(self) -> c_void_p:
        status = self._dll.sc5507n8a_psgOpenDevice(
            USB_INT,
            self.serial_number.encode("ascii"),
            c_uint8(0),              # baudrate: ignored on USB
            byref(self._handle),
        )
        _check("OpenDevice(serial={!r})".format(self.serial_number), status)
        if not self._handle.value:
            raise RuntimeError(
                "OpenDevice returned SCI_SUCCESS but handle is NULL for "
                "serial {!r}.".format(self.serial_number)
            )
        return self._handle

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle.value:
            try:
                self._dll.sc5507n8a_psgCloseDevice(self._handle)
            except Exception:
                # Don't mask an earlier exception.
                pass
            self._handle = c_void_p()


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------
def turn_on(serial_number: str) -> None:
    """Enable the RF output on the device with the given serial number."""
    dll = _load_dll()
    with _Connection(serial_number) as handle:
        status = dll.sc5507n8a_psgSetOutputEnable(handle, c_uint8(0), c_uint8(1))
        _check("SetOutputEnable(pulse=0, out=1)", status)


def turn_off(serial_number: str) -> None:
    """Disable the RF output on the device with the given serial number."""
    dll = _load_dll()
    with _Connection(serial_number) as handle:
        status = dll.sc5507n8a_psgSetOutputEnable(handle, c_uint8(0), c_uint8(0))
        _check("SetOutputEnable(pulse=0, out=0)", status)


def set_frequency(serial_number: str, frequency_hz: float) -> None:
    """Set the output frequency in Hz (e.g. 3.2e9 for 3.2 GHz).

    The device accepts DC to 6.25 GHz.

    Raises RuntimeError if the device is currently armed in listen mode --
    the caller must run stop_listening_mode first.  See start_listening_mode.
    """
    dll = _load_dll()
    with _Connection(serial_number) as handle:
        # Guard: do not silently overwrite frequency while armed.  Reading
        # operate_status.rf_mode tells us whether list/sweep mode is engaged.
        dev_status = device_status_t()
        status = dll.sc5507n8a_psgFetchDeviceStatus(handle, byref(dev_status))
        _check("FetchDeviceStatus", status)
        if dev_status.operate_status.rf_mode:
            raise RuntimeError(
                "Device is in listen mode. Call stop_listening_mode(serial) "
                "first if you want to use set_frequency."
            )

        status = dll.sc5507n8a_psgSetFrequency(
            handle, c_double(float(frequency_hz))
        )
        _check("SetFrequency({} Hz)".format(frequency_hz), status)


def start_listening_mode(serial_number,
                         frequencies_hz,
                         trigger_edge="rising",
                         power_dbm=None):
    """Load a frequency list and arm the device to step one frequency per TTL edge.

    On every trigger edge (rising or falling) at the hardware trigger input,
    the device advances to the next frequency in ``frequencies_hz``.  After
    the last entry, it wraps back to the first and keeps running indefinitely.

    Parameters
    ----------
    serial_number : str
        8-char hex serial on the SC5508A product label.
    frequencies_hz : iterable of float
        List of frequencies in Hz.  Each must be in [100 MHz, 6.25 GHz] and
        the list length must be in [1, 2048].  100 MHz lower bound is a DLL
        buffer constraint (ListBufferWrite), not a device-hardware constraint.
    trigger_edge : {"rising", "falling"}, optional
        Which TTL edge advances the frequency.  Defaults to "rising".
    power_dbm : float, optional
        RF power applied to every list entry.  If None (default), the current
        device power level is queried and re-applied to all points so entering
        listen mode does not change the amplitude.

    Side effects
    ------------
    After this call the device is in list/sweep RF mode.  ``set_frequency``
    will refuse to run until ``stop_listening_mode`` is called.
    """
    freqs = [float(f) for f in frequencies_hz]
    n = len(freqs)
    if n == 0:
        raise ValueError("frequencies_hz is empty")
    if n > _LIST_MAX_POINTS:
        raise ValueError(
            "frequencies_hz has {} entries; device buffer holds at most {}".format(
                n, _LIST_MAX_POINTS
            )
        )
    for f in freqs:
        if not (_LIST_MIN_FREQ_HZ <= f <= _MAX_FREQ_HZ):
            raise ValueError(
                "frequency {} Hz is outside the list-mode range "
                "[{:g}, {:g}] Hz".format(f, _LIST_MIN_FREQ_HZ, _MAX_FREQ_HZ)
            )

    edge = str(trigger_edge).lower()
    if edge not in ("rising", "falling"):
        raise ValueError(
            "trigger_edge must be 'rising' or 'falling', got {!r}".format(
                trigger_edge
            )
        )
    edge_byte = 1 if edge == "rising" else 0

    dll = _load_dll()
    with _Connection(serial_number) as handle:
        # Decide per-point power level.
        if power_dbm is None:
            rf_params = device_rf_params_t()
            status = dll.sc5507n8a_psgFetchRfParameters(
                handle, byref(rf_params)
            )
            _check("FetchRfParameters", status)
            level = float(rf_params.power_level)
        else:
            level = float(power_dbm)

        # Build the freq / amplitude arrays.
        FreqArr = c_double * n
        LevelArr = c_float * n
        freq_arr = FreqArr(*freqs)
        level_arr = LevelArr(*([level] * n))

        # 1. Load the list buffer.
        status = dll.sc5507n8a_psgListBufferWrite(
            handle, freq_arr, level_arr, c_int(n)
        )
        _check("ListBufferWrite(n={})".format(n), status)

        # 2. Configure list-mode behavior: HW-triggered, step per trigger,
        #    wrap to first, no trigger-out.
        mode = list_mode_t(*_LIST_MODE_STEP_WRAP)
        status = dll.sc5507n8a_psgListModeConfig(handle, byref(mode))
        _check("ListModeConfig", status)

        # 3. Configure the hardware trigger edge. USB => pxi_enable=0.
        trig = hw_trigger_t(c_uint8(edge_byte), c_uint8(0), c_uint8(0))
        status = dll.sc5507n8a_psgListHwTrigConfig(handle, byref(trig))
        _check("ListHwTrigConfig(edge={})".format(edge), status)

        # 4. Continuous cycling (0 = run forever).
        status = dll.sc5507n8a_psgListCycleCount(handle, c_uint32(0))
        _check("ListCycleCount(0=continuous)", status)

        # 5. Dwell time: irrelevant when step_on_hw_trig=1, but some firmware
        #    revisions reject a zero dwell.  2 ticks = 1 ms is a safe default.
        status = dll.sc5507n8a_psgSweepDwellTime(handle, c_uint32(2))
        _check("SweepDwellTime(2=1ms)", status)

        # 6. Arm: flip the RF engine to list/sweep mode.  The device is now
        #    holding at the first list entry and waiting for TTL edges.
        status = dll.sc5507n8a_psgSetRfMode(handle, c_uint8(1))
        _check("SetRfMode(1=list)", status)


def stop_listening_mode(serial_number: str) -> None:
    """Return the device from list/sweep mode to single-tone CW mode.

    Does not toggle the RF output enable; if the output was on it stays on.
    After this call ``set_frequency`` works normally again.
    """
    dll = _load_dll()
    with _Connection(serial_number) as handle:
        status = dll.sc5507n8a_psgSetRfMode(handle, c_uint8(0))
        _check("SetRfMode(0=single-tone)", status)


# -----------------------------------------------------------------------------
# Smoke test. Run `python signalcore_control.py <serial>` to exercise every
# public function against a real device.  The listen-mode step waits 10 s so
# you can send TTL pulses into the hardware trigger input and watch the
# spectrum analyzer step through [2, 3, 4, 5] GHz, wrapping back to 2 GHz.
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import time

    if len(sys.argv) != 2:
        print("Usage: python signalcore_control.py <8-char-hex-serial>")
        print("  (serial number is printed on the product label)")
        sys.exit(1)

    sn = sys.argv[1]
    print("Loading DLL from: {}".format(_DLL_PATH))
    print("Using device serial: {}".format(sn))

    # --- [1] Baseline: single-tone set_frequency + turn_on ---
    print("\n[1/6] Setting frequency to 1.0 GHz (single-tone) ...")
    set_frequency(sn, 1.0e9)

    print("[2/6] Turning RF output ON ...")
    turn_on(sn)
    time.sleep(1.0)

    # --- [3] Enter listen mode ---
    test_freqs = [2.0e9, 3.0e9, 4.0e9, 5.0e9]
    print("[3/6] Entering listen mode; list = {} Hz, rising-edge TTL".format(
        test_freqs
    ))
    start_listening_mode(sn, test_freqs, trigger_edge="rising")
    print("      Device is armed. Send TTL pulses to the hardware trigger")
    print("      input now -- each rising edge should step to the next")
    print("      frequency, wrapping 2 -> 3 -> 4 -> 5 -> 2 GHz.")
    print("      Waiting 10 s for you to pulse the trigger ...")
    time.sleep(10.0)

    # --- [4] Guard: set_frequency must refuse while armed ---
    print("[4/6] Verifying set_frequency is blocked while in listen mode ...")
    try:
        set_frequency(sn, 1.0e9)
    except RuntimeError as err:
        print("      OK -- got expected error: {}".format(err))
    else:
        print("      FAIL -- expected RuntimeError, got none!")

    # --- [5] Leave listen mode, verify set_frequency works again ---
    print("[5/6] Stopping listen mode and setting frequency to 1.5 GHz ...")
    stop_listening_mode(sn)
    set_frequency(sn, 1.5e9)
    time.sleep(1.0)

    # --- [6] Clean up ---
    print("[6/6] Turning RF output OFF ...")
    turn_off(sn)

    print("\nDone.")
