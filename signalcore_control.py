"""
signalcore_control.py
---------------------
Minimal Python control for SignalCore SC5507A / SC5508A signal generators
over USB, using ctypes to call the vendor-supplied 64-bit Windows DLL
(sc5507n8a_psg.dll).

Three functions are exposed:
    turn_on(serial_number)            -> enable RF output
    turn_off(serial_number)           -> disable RF output
    set_frequency(serial_number, hz)  -> set output frequency in Hz

Requirements:
    - 64-bit Python on Windows
    - SignalCore driver installed so that sc5507n8a_psg.dll is on disk
    - libusb-1.0.dll reachable on the DLL search path (SignalCore's
      installer normally handles this)
    - winusb.sys bound to the device (handled by the installer)

Function signatures below are confirmed against:
    - `dumpbin /exports sc5507n8a_psg.dll` (for exact exported symbol names)
    - SignalCore's shipped example.cpp (for argument counts and types)
    - The SC5507A/SC5508A Programming Manual Rev 1.1 (for meaning)

Note: the vendor's example.cpp calls `SetOutputEnable(handle, 0, 1)` — that
is, the function takes THREE arguments (handle, pulse_mode, output_enable),
not two as the manual text suggests. The first extra arg controls pulse
modulation capability; we pass 0 (disabled) for straight CW operation.
"""

import ctypes
from ctypes import (
    c_char_p,
    c_double,
    c_int,
    c_uint8,
    c_void_p,
    byref,
    POINTER,
)
import os

# -----------------------------------------------------------------------------
# Configuration: update this path to wherever the SignalCore installer put the
# 64-bit DLL on your machine.
# -----------------------------------------------------------------------------
_DLL_PATH = r"C:\Program Files\SignalCore\SC5507_8A\api\c\x64\sc5507n8a_psg.dll"

# -----------------------------------------------------------------------------
# Constants (from the manual Appendix 7.1)
# -----------------------------------------------------------------------------
SCI_SUCCESS = 0

# sci_comm_interface_t enum
PCI_INT = 0
USB_INT = 1
RS232_INT = 2

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
            f"Could not find SignalCore DLL at:\n  {_DLL_PATH}\n"
            "Edit _DLL_PATH at the top of signalcore_control.py to point at "
            "your installed sc5507n8a_psg.dll (64-bit)."
        )

    # All exported functions are __cdecl per the manual, so use CDLL.
    dll = ctypes.CDLL(_DLL_PATH)

    # --- sc5507n8a_psgOpenDevice ---
    # SCISTATUS OpenDevice(sci_comm_interface_t commInterface,
    #                      char *id,
    #                      uint8_t baudrate,
    #                      HANDLE *deviceHandle);
    # For USB: `id` is the 8-char hex serial, `baudrate` is ignored.
    dll.sc5507n8a_psgOpenDevice.argtypes = [
        c_int,               # commInterface
        c_char_p,            # id (serial number string)
        c_uint8,             # baudrate (ignored on USB)
        POINTER(c_void_p),   # deviceHandle (out)
    ]
    dll.sc5507n8a_psgOpenDevice.restype = c_int

    # --- sc5507n8a_psgCloseDevice ---
    # SCISTATUS CloseDevice(HANDLE deviceHandle);
    dll.sc5507n8a_psgCloseDevice.argtypes = [c_void_p]
    dll.sc5507n8a_psgCloseDevice.restype = c_int

    # --- sc5507n8a_psgSetFrequency ---
    # SCISTATUS SetFrequency(HANDLE deviceHandle, double freq_hz);
    # Confirmed via example.cpp: takes a double in Hz.
    dll.sc5507n8a_psgSetFrequency.argtypes = [c_void_p, c_double]
    dll.sc5507n8a_psgSetFrequency.restype = c_int

    # --- sc5507n8a_psgSetOutputEnable ---
    # SCISTATUS SetOutputEnable(HANDLE deviceHandle,
    #                           uint8_t pulse_mode,
    #                           uint8_t output_enable);
    # The example passes 3 args: (handle, 0, 1) to enable with pulse off.
    dll.sc5507n8a_psgSetOutputEnable.argtypes = [c_void_p, c_uint8, c_uint8]
    dll.sc5507n8a_psgSetOutputEnable.restype = c_int

    _dll = dll
    return _dll


def _check(name: str, status: int) -> None:
    """Raise if a SignalCore call returned non-success."""
    if status != SCI_SUCCESS:
        raise RuntimeError(
            f"SignalCore {name!r} failed with SCISTATUS={status}"
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
        _check(f"OpenDevice(serial={self.serial_number!r})", status)
        if not self._handle.value:
            raise RuntimeError(
                f"OpenDevice returned SCI_SUCCESS but handle is NULL for "
                f"serial {self.serial_number!r}."
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
    """Enable the RF output on the device with the given serial number.

    Note: per the manual, SetOutputEnable is the 'fast' enable path — it
    maximises attenuation and parks oscillators at a low-leakage frequency
    but does NOT fully power them down, so low-level LO leakage may still
    appear at the output when 'off'. For full RF silencing, use the standby
    function (not wrapped here).
    """
    dll = _load_dll()
    with _Connection(serial_number) as handle:
        # (handle, pulse_mode=0, output_enable=1)  -- matches example.cpp
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
    """
    dll = _load_dll()
    with _Connection(serial_number) as handle:
        status = dll.sc5507n8a_psgSetFrequency(handle, c_double(float(frequency_hz)))
        _check(f"SetFrequency({frequency_hz} Hz)", status)


# -----------------------------------------------------------------------------
# Smoke test. Run `python signalcore_control.py <serial>` to verify the DLL
# loads and basic control works end-to-end.
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import time

    if len(sys.argv) != 2:
        print("Usage: python signalcore_control.py <8-char-hex-serial>")
        print("  (serial number is printed on the product label)")
        sys.exit(1)

    sn = sys.argv[1]
    print(f"Loading DLL from: {_DLL_PATH}")
    print(f"Using device serial: {sn}")

    print("Setting frequency to 1.0 GHz...")
    set_frequency(sn, 1.0e9)

    print("Turning output ON...")
    turn_on(sn)

    time.sleep(1.0)  # let you glance at the spectrum analyzer if you have one

    print("Turning output OFF...")
    turn_off(sn)

    print("Done.")
