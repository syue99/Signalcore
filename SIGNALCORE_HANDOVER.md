# SignalCore SC5507A / SC5508A — Python Integration Handover

This document captures everything we learned while getting the SignalCore
SC5507A / SC5508A signal generator controllable from Python in the lab. It
exists so the next person (or future-you) extending `signalcore_control.py`
does not have to re-derive the API shape, re-verify exported symbols, or
re-debug the environment setup.

**Companion file:** `signalcore_control.py` — a minimal three-function
wrapper (`turn_on`, `turn_off`, `set_frequency`) built on the findings
below.

---

## 1. Device overview

- **Product:** SignalCore SC5507A (PXIe) and SC5508A (USB / RS232). Same
  silicon, same API, different host interface. We use the **SC5508A over USB**.
- **RF range:** DC to 6.25 GHz.
- **Power range:** typically -50 dBm to +15 dBm, 0.01 dB steps.
- **Onboard power sensor:** 1 MHz to 6 GHz, ±0.5 dB.
- **Host connection:** USB 2.0 full-speed via mini-USB Type B on the front
  panel. Windows only for programmatic control (see §4).

---

## 2. The key discovery: no Python API, no VISA, no SCPI

SignalCore ships a Windows C SDK (DLL + header + `.lib`) and example code in
C/C++ and LabVIEW — **no Python bindings, no USBTMC/VISA support, no SCPI
over a virtual COM port**. This rules out the usual instrument-control
shortcuts:

- `pyvisa` will not find the device — it is not a VISA-compliant endpoint.
  The USB path goes through WinUSB and SignalCore's own libusb-based
  protocol, not USBTMC.
- `pyserial` could in principle drive the RS232 port, but you would be
  reverse-engineering the register protocol. Not worth it when the DLL
  already exists.

**The correct approach is `ctypes` wrapping the vendor DLL.** That is what
`signalcore_control.py` does.

---

## 3. Files SignalCore ships (in the install folder)

From the lab install directory:

| File | Purpose | Date seen |
|---|---|---|
| `sc5507n8a_psg.dll` | The runtime library we call from Python. 32 KB. | 2023-03-20 |
| `sc5507n8a_psg.lib` | Import stub for C/C++ linkers. **Not used from Python.** | 2016-03-16 |
| `sc5507n8a_psg.exp` | Export manifest. Informational. | 2023-03-20 |
| `sc5507n8a_psg.pdb` | Debug symbols for the DLL. Not needed at runtime. | 2023-03-20 |
| `sc5508a_usb_example.exe` | Compiled vendor example. Useful sanity check. | 2020-11-17 |
| `example.cpp` (in examples folder) | **Ground truth for API usage.** | — |

Note the mismatched dates: the `.dll` is 2023 but the `.lib` is 2016. This
is fine for us because we bind symbols dynamically via `ctypes.CDLL` at
runtime rather than linking against the `.lib`. Flagging it only in case a
future C/C++ integration hits "unresolved symbol" errors — the stale `.lib`
would be the first suspect.

---

## 4. Environment constraints

| Constraint | Why |
|---|---|
| **Windows only** | The DLL and its kernel-level dependencies (`winusb.sys`, `libusb-1.0.dll`) are Windows-specific. |
| **64-bit Python** | The DLL is 64-bit. Mismatched bitness fails with cryptic "module not found" errors from `CDLL`. Verify with `python -c "import struct; print(struct.calcsize('P')*8)"` — must print `64`. |
| **Python 3.6** | **Hard constraint from the rest of the lab codebase.** See §9. |
| `libusb-1.0.dll` on PATH | The vendor DLL depends on it. SignalCore's installer normally puts it alongside `sc5507n8a_psg.dll`, which works because Windows DLL search includes the loading DLL's directory. If you see `OSError: [WinError 126]` when loading, check this. |
| `winusb.sys` bound to the device | Device Manager should show the SC5508A under WinUSB/libusb devices, not as "Unknown Device" or a generic COM port. The SignalCore installer handles this. If in doubt, the vendor's GUI program is a good canary — if their GUI can see the device, we can too. |

---

## 5. Ground truth for the API: dumpbin + example.cpp

The programming manual is mostly right but has two concrete errors and one
naming-convention issue that cost us time. The canonical sources are:

1. **`dumpbin /exports sc5507n8a_psg.dll`** — tells you the exact exported
   symbol names.
2. **SignalCore's shipped `example.cpp`** — tells you the real argument
   counts and types because it actually compiles and runs against the DLL.

When in doubt, believe these two over the PDF manual.

### 5.1 Full export list (from `dumpbin /exports` on our installed DLL)

All 44 functions use the prefix `sc5507n8a_psg` with the function name
appended in **CamelCase, no separator** — e.g. `sc5507n8a_psgSetFrequency`.
The manual describes them as "`SetFrequency`"; the prefix is always
present in the real symbol.

```
sc5507n8a_psgCloseDevice              sc5507n8a_psgOpenDevice
sc5507n8a_psgFetchDeviceInfo          sc5507n8a_psgOpenDeviceLV
sc5507n8a_psgFetchDeviceStatus        sc5507n8a_psgRegRead
sc5507n8a_psgFetchHwTrigConfig        sc5507n8a_psgRegWrite
sc5507n8a_psgFetchLevelDacValue       sc5507n8a_psgSearchDevices
sc5507n8a_psgFetchRfParameters        sc5507n8a_psgSearchDevicesLV
sc5507n8a_psgFetchSensorLevel         sc5507n8a_psgSetAlcMode
sc5507n8a_psgFetchTemperature         sc5507n8a_psgSetAttenDirect
sc5507n8a_psgInitDevice               sc5507n8a_psgSetAutoLevelDisable
sc5507n8a_psgListBufferPoints         sc5507n8a_psgSetDeviceStandby
sc5507n8a_psgListBufferRead           sc5507n8a_psgSetFrequency
sc5507n8a_psgListBufferTransfer       sc5507n8a_psgSetLevelDacValue
sc5507n8a_psgListBufferWrite          sc5507n8a_psgSetOutputEnable
sc5507n8a_psgListCycleCount           sc5507n8a_psgSetPowerLevel
sc5507n8a_psgListHwTrigConfig         sc5507n8a_psgSetReferenceDacValue
sc5507n8a_psgListModeConfig           sc5507n8a_psgSetReferenceMode
sc5507n8a_psgListSoftTrigger          sc5507n8a_psgSetRfMode
                                      sc5507n8a_psgSetSensorConfig
                                      sc5507n8a_psgSetSensorFrequency
                                      sc5507n8a_psgSetSignalOffsetPhase
                                      sc5507n8a_psgSetSynthMode
                                      sc5507n8a_psgStoreDefaultState
                                      sc5507n8a_psgSweepDwellTime
                                      sc5507n8a_psgSweepStartFreq
                                      sc5507n8a_psgSweepStepFreq
                                      sc5507n8a_psgSweepStopFreq
                                      sc5507n8a_psgSynthSelfCalibrate
```

The `*LV` variants are LabVIEW-specific and not needed from Python.

### 5.2 Confirmed signatures (the four we actually use)

All functions return `SCISTATUS` (a C `int`, 0 = success) **except** where
noted. All are `__cdecl`.

| Function | Signature | Notes |
|---|---|---|
| `OpenDevice` | `(commInterface: int, id: char*, baudrate: uint8, handle: HANDLE*) -> int` | 4 args. `commInterface` is an enum (PCI=0, USB=1, RS232=2). For USB, `id` is the 8-char hex serial; `baudrate` ignored. Handle written through the pointer. |
| `CloseDevice` | `(handle: HANDLE) -> int` | |
| `SetFrequency` | `(handle: HANDLE, freq: double) -> int` | Frequency in Hz as a `double`. |
| `SetOutputEnable` | `(handle: HANDLE, pulse_mode: uint8, output_enable: uint8) -> int` | **Three args, not two.** The manual text is wrong here. Pass `pulse_mode=0` for straight CW. |

### 5.3 Where the manual misled us

| Manual says | Reality | How we found out |
|---|---|---|
| `SetOutputEnable(handle, enable)` (2 args) | `(handle, pulse_mode, output_enable)` — 3 args | `example.cpp` calls it as `SetOutputEnable(dev_handle, 0, 1)` |
| Function names are plain CamelCase, no prefix | Real exports all prefixed `sc5507n8a_psg` | `dumpbin /exports` |
| (Manual typo / copy-paste error in §4.7.1) | `SetPowerLevel` takes `(handle, dBm)` | Cross-check with `example.cpp` |

### 5.4 A rabbit hole we went down (for future context)

We initially looked at ShabaniLab's Labber driver for the SC5511A as a
Python reference. **Do not blindly port from it.** Lessons:

- SC5511A has a different, simpler DLL API: `sc5511a_open_device(serial)`
  returns a handle directly, `sc5511a_set_freq(handle, uint64_hz)` takes
  integer Hz. Our device does *neither* of these things.
- The ShabaniLab file is still useful for the general `ctypes` mechanics
  (how to bind signatures, `POINTER`, struct layouts) and for a sense of
  what a higher-level driver class can look like.
- Lesson: **the API shape varies between SignalCore product lines.** The
  SC5511A API is not the SC5507A/8A API. When extending support to any new
  SignalCore model in the future, re-run `dumpbin` and re-read the example
  `.cpp` for that product.

---

## 6. The wrapper design (what `signalcore_control.py` does)

Current file scope: minimal. Three top-level functions — `turn_on`,
`turn_off`, `set_frequency` — each opens the USB connection, issues one
call, closes. Plus an internal `_Connection` context manager and a
`_load_dll` cache.

Design choices worth preserving when extending:

1. **Lazy DLL loading.** `_load_dll()` is called on first use and caches
   the `CDLL` object. Lets `import signalcore_control` succeed on
   machines without the driver (e.g. CI, documentation builds).
2. **Explicit `argtypes` / `restype` on every function.** ctypes defaults
   to `int`-sized args and return values. On 64-bit Windows that silently
   corrupts `HANDLE` (should be pointer-sized) and `double` arguments.
   **Always set signatures before calling any function.**
3. **`HANDLE` is `c_void_p`.** Correct for Windows `HANDLE` on x64.
4. **Status-code checking via `_check()`.** Every call raises
   `RuntimeError` on non-zero `SCISTATUS`, with the name of the call and
   the status value. Much nicer than silent failure.
5. **Open → act → close on every public call.** Simple and safe for a
   three-function wrapper. Costs ~tens of ms per call. Fine for setup
   scripts, not fine for tight loops. See §8 for the refactor plan.

---

## 7. How to run it

1. Update `_DLL_PATH` at the top of `signalcore_control.py` to match where
   the SignalCore installer put the 64-bit DLL on your machine.
2. Find the device serial number (8 hex characters like `100E4FC2`) on the
   product label.
3. Run: `python signalcore_control.py <SERIAL>`

The built-in smoke test sets 1 GHz, turns the output on, waits a second,
turns it off.

### Common first-run failures

| Symptom | Likely cause | Fix |
|---|---|---|
| `FileNotFoundError` at `_DLL_PATH` | Wrong path | Find the real path to `sc5507n8a_psg.dll` and update the constant. |
| `OSError: [WinError 126]` on `CDLL(...)` | Missing `libusb-1.0.dll` dependency | Ensure it is in the same folder as the SignalCore DLL or on `PATH`. |
| `AttributeError: function 'xxx' not found` | Symbol name typo | Re-check against §5.1. |
| `SCISTATUS=non-zero` from `OpenDevice` | Driver / device enumeration issue | Open Device Manager; confirm SC5508A appears under WinUSB devices. Run SignalCore's own GUI as a canary. |
| `SyntaxError: future feature annotations is not defined` | `from __future__ import annotations` on Python 3.6 | Remove that line (see §9). Already handled in current file. |

---

## 8. Roadmap for future features

When extending the wrapper, the pattern is:

1. Find the function in §5.1 or re-run `dumpbin /exports` if the DLL was
   updated.
2. Find how `example.cpp` calls it (or the closest analogous call) to
   confirm argument types/counts — remember the manual is sometimes wrong.
3. Add an entry to `_load_dll()` setting `.argtypes` and `.restype`.
4. Add a Python-facing wrapper function that uses `_Connection` and
   `_check`.
5. For functions returning data via a struct pointer (e.g.
   `FetchDeviceStatus`, `FetchRfParameters`, `FetchDeviceInfo`): define a
   `ctypes.Structure` subclass mirroring the layout from the manual
   Appendix 7.1, pass `byref(my_struct)` to the call, read fields out.

### Likely next features (in priority order)

1. **Power level control** — `SetPowerLevel(handle, float dBm)`. One-liner.
2. **Reference clock config** — `SetReferenceMode(handle, pxi10, high_out, lock_ext)`. Matters for phase coherence with other instruments.
3. **Read back current state** — `FetchRfParameters(handle, &struct)` → frequency, power, sweep params. Requires defining `device_rf_params_t` as a `ctypes.Structure`.
4. **Standby / full off** — `SetDeviceStandby(handle, standby)`. `SetOutputEnable(0)` only does the "fast off" which leaves oscillators running; LO leakage may be visible on a sensitive analyzer. For full RF silencing, standby is the answer.
5. **Power sensor readback** — `SetSensorConfig`, `SetSensorFrequency`, `FetchSensorLevel`. Gives you a closed-loop power measurement without an external power meter.
6. **Refactor to a class** — `SC5508A(serial_number)` as a context manager holding the connection open across many operations. The current open-on-every-call design thrashes the front-panel LED and costs latency in any loop. When refactoring, keep the standalone top-level functions as thin wrappers over the class for backward compatibility, or plan a migration.
7. **List / sweep mode** — the largest chunk of unused functionality. Requires wrapping `ListModeConfig`, `ListBufferWrite`, `SweepStartFreq/StopFreq/StepFreq`, `SweepDwellTime`, `ListCycleCount`, `ListSoftTrigger`. Useful for fast frequency sweeps without per-point software round-trips.

### Struct layouts to reference when wrapping Fetch functions

From the manual Appendix 7.1, these are the key structs. All members are
`uint8` unless noted; `HANDLE` is `c_void_p`.

- `device_info_t` — serial number, firmware/hardware rev, manufacture date
- `pll_status_t` — 8 lock-detect flags
- `operate_status_t` — ~20 one-byte flags for modes and lock states
- `list_mode_t` — 8 one-byte sweep config fields
- `device_status_t` — bundles the three above
- `device_rf_params_t` — frequencies (doubles), power (float), sweep params (uint32)
- `synth_mode_t` — 5 one-byte synthesizer config fields

---

## 9. Python 3.6 constraint

**The rest of the lab codebase is pinned to Python 3.6**, and a broader
dependency migration is happening slowly as a separate effort. Anything
added to `signalcore_control.py` must run on 3.6. Concretely:

- ✅ **f-strings** (3.6+) — fine.
- ✅ **Variable annotations** like `x: int = 0` (3.6+) — fine.
- ✅ **Function type hints** — fine.
- ❌ **`from __future__ import annotations`** — 3.7+. Removing this was
  the fix for the first runtime error we hit.
- ❌ **`list[str]`, `dict[str, int]`** as type hints outside strings —
  3.9+. Use `List[str]` from `typing` instead if you want hints.
- ❌ **Walrus operator `:=`** — 3.8+.
- ❌ **Positional-only parameters (`/`)** — 3.8+.
- ❌ **`os.add_dll_directory(...)`** — 3.8+. If we need to point the DLL
  loader at a specific folder, modify `PATH` in the environment or drop
  the dependency next to the main DLL.
- ⚠️ **dataclasses** — 3.7+; available on 3.6 via the `dataclasses`
  backport on PyPI. Avoid unless we really need them.

### One note about 3.6 and the underlying Windows runtime

Python 3.6 has been EOL since December 2021. The SignalCore DLL itself
does not care about the Python version (it's pure C), but `libusb-1.0.dll`
bundled with the installer may depend on VC++ runtimes newer than whatever
shipped with the original 3.6 install. If we see strange DLL-load errors
that don't point at a specific missing file, installing the latest
Microsoft Visual C++ Redistributable on the machine is a cheap thing to
try.

---

## 10. Useful commands / paths (fill these in for your machine)

```
DLL path:           C:\Program Files\SignalCore\SC5507_8A\...\sc5507n8a_psg.dll
Device serial:      <8 hex chars from product label>
Python env:         C:\Users\<user>\.conda\envs\code3\python.exe   (Python 3.6)
Project root:       C:\Users\<user>\Documents\Codebase\Lab_control
This file:          servers\control_instrument_servers\signalcore_control.py
```

To re-verify DLL exports at any time:

```
dumpbin /exports "C:\path\to\sc5507n8a_psg.dll"
```

(Requires Visual Studio's developer command prompt. Alternatively, `pip
install pefile` and use `pefile` from Python — works on 3.6.)

---

## 11. TL;DR for the next person

- It's an RF signal generator controlled via a vendor Windows DLL.
- Python talks to it via `ctypes`. **Not** pyvisa, **not** SCPI.
- Function symbols are prefixed `sc5507n8a_psg` + CamelCase name — see §5.1.
- When adding features, trust `dumpbin` and `example.cpp` over the manual.
- Lab is on **Python 3.6** (§9). No `from __future__ import annotations`, no `list[str]` hints.
- Start by reading `signalcore_control.py`; extend using the pattern in §8.
