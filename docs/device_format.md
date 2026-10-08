# SCOPE devices (.dev): structure, wiring, parameters, and how to run them through pulsard

This document covers three things:

- How a SCOPE device file is put together: the RODModule tree, DSP atoms, host-side "pep" scripts, pads, nets
  and routing switches.
- How a user parameter (a knob, fader or button) gets from its display value to the raw 32-bit word written to a
  DSP pad, including curves, units and sample-rate scaling.
- How `tools/scope_device.py` turns a `.dev` into a load plan for `pulsard`.

Prerequisites: `io_format.md` (S3 container and object archive, decoded by `tools/scope_dev.py`) and
`module_loading.md` (linking and wiring one DSP module).

Legend:

- **[C]** = read from the code. **[L]** = likely. **[?]** = guess. **[V]** = verified on the whole corpus with
  `scope_device.py survey`.
- `FUN_x` / `Name @x` = function at address x. Image bases: base.dll 0x10000000, Sim2k.dll 0x10c00000,
  PepBasic.dll 0x10500000, script.dll 0x10000000, pp.dll 0x10000000. The Ghidra output of script.dll, pp.dll,
  PepBasic.dll and PepNBasic.dll was added to `../decompiled/` for this work.
- "pep script" = a host-side class written in SCOPE's Java-like script language ("Lava"), stored in
  `App/Script/*.pep` (see §4.1).

This document describes formats and summarizes behaviour in its own words. No SCOPE file contents are
reproduced.

---------------------------------------------------------------------------------------------------
## 0. TL;DR

1. **Two kinds of leaf module [C].** Every leaf of a device's RODModule tree has an algo that is one of:
   - a `ROCAtom`: a DSP module, matched by name to a `.dsp` file;
   - a `ROCAlgo` with a `pep` class name: a host-side script. Scripts include knobs (`NewAni`), faders, buttons,
     text displays, value converters (`Long2LongSyncAtom`, ...), routing switches (`SignalSwitch`, ...) and GUI
     elements.

   Of the 72k modules in the 211 devices, about 10% (7.6k) are atoms. Everything else runs on the host.
2. **Nets, not wires [C/V].** A `RODRouting` joins two `RODPad`s, and a `RODPad.link` joins a pad to the atom or
   script pad that it exposes. Together these form undirected **nets**:
   - A net with one DSP output pad and some DSP input pads is a DSP wire set.
   - A net that contains a pad of the device root is an **external port**.
   - A net with DSP inputs but no DSP output is fed by a host value: either a parameter, or the constant saved in
     the file.

   Ids are unique per file, so nets are a plain union-find over ids.
3. **Parameters are host scripts [C].** A knob is a `ScriptController` subclass. It maps knob position x ∈ [0,1]
   through a curve to `Val = Min + curve(x)·(Max−Min)`. `Val` flows through converter scripts to the DSP input
   pad.

   A text display (`ScriptTextController` family) sits on the same `Val` net. It shows
   `Min_t + curve_t((Val−MinControl)/(MaxControl−MinControl))·(Max_t−Min_t)`, optionally followed by
   `(d+Offset)/Divisor`, formatted with a printf `Format` such as `%1.0fHz`.

   Recomputing every saved text string of the corpus from the saved values matches **5397 of 5406 [V]**.
4. **Units and the sample rate [C].** Atom pad values are stored for a **48 kHz reference**. When base.dll
   writes a pad (`ROCAtom::SetInPad` @10075ce0 → `FUN_10075b40`), it scales by the pad's `Unit` attribute:
   - Unit 1 (frequency): `raw = v·48000/fs`. The value is a phase increment, INT_MAX = Nyquist at 48 kHz, so
     `raw = F/fs·2^32`.
   - Unit 2 (time): `raw = v·fs/48000`. The value is in samples at 48 kHz.
   - Other units: written unchanged.

   `UnitAttribute::sampleRate` is set by `PepDisplayObject::SetSampleRate` (PepBasic @105167a0), which then calls
   `ROCAtom::ChangeSampleRate` to resend the values.
5. **What blocks a device [V].** Of the 211 `.dev` files, 95 give a complete plan; 36 of those need neither MIDI
   nor a license atom. The blockers are:
   - built-in PC/host atoms without a `.dsp`: the "PC Master 32k/4k/256k Delay" family is in 50 devices, and
     DLL/VxD atoms appear in samplers and recorders (68 devices in total);
   - voice-array pads of MIDI Voice Control and voice mixers: every synth;
   - polyphonic `VoiceDef` modules;
   - 39 devices that have no DSP module at all.
6. **Recommended first targets (§7):** High Cut M / Low Cut M, Phaser M, Flanger M, 4-Pole M and Chorus S. No
   mono synth is possible yet. EDS8i (drum synth) plans completely but needs a MIDI input.

---------------------------------------------------------------------------------------------------
## 1. Device structure

### 1.1 The tree [C]
A `.dev` holds one root `RODBase` (`RODModule::Serialize` @100c27c0, see io_format.md §2.4). The root's algo is
usually a `ROCAlgo` with pep `EffectInsert`, which holds bookkeeping pads such as NumChannels, IsExternal and
cwDeviceName. Below the root are:

| child | algo | role |
|---|---|---|
| DSP leaf | `ROCAtom` (`module` = DSP module name) | one DSP module instance; its vars are `ROCAtomPad`s |
| script leaf | `ROCAlgo` with `pep` = script class | host logic (§4) |
| container | `ROCAlgo` pep `Group`, `Surfaces@BasicSurface`, ... or none | hierarchy only; the panel lives below the `Surfaces@...` module |
| `<RefModule>` | (another file, `SerializeRefInformation` @100bee90) | not used by any `.dev`, only by projects |

Each module also has `RODPad`s (its visible pads), optional `ROCParamList`s (§5.3) and `RODRouting`s between pads
of its subtree.

### 1.2 Atom name → `.dsp` file [C]
The lookup is Sim2k `newModule` @10c03ef0 → `FUN_10c02ba0`. For each registered class it compares the name
against, in order:

1. seg_name string 0 (short name, class+0x18);
2. seg_name string 1 (long name, class+0x1c);
3. the base name of the class's file (class+0x20), case-insensitive.

Many devices use that last form, for example `fms2a.dsp`. If nothing matches, base.dll's `CallBackLoadAtom` is
tried, and the name may also be retried with a " DLL"/" VxD" suffix (`FUN_10c03fe0`). That path covers the
**host-side atoms**:

- `pc_delay32k/4k/256k`, `pc_erDelay`, `sdram_delay*` (`Sim2k atoms\*.cpp`);
- DLL atoms in `App/Dll`.

These have no `.dsp` file and cannot be loaded on Linux yet. For example, "PC Master 32k Delay" is a delay line
whose memory is managed by Sim2k on the PC [L].

### 1.3 Placement [C/L]
- **Fixed DSP**: the DSP module's own flags `(flags>>17)&0xF` − 1 (module_loading.md, `FUN_10c260b0`). None of
  the effect modules in the recommended devices are fixed.
- **Extras chunk 0x10f** of the root (`SaveDSPPlacement` @10056320): a per-module DSP assignment.
  - `-1` = free, which is almost everywhere.
  - 19 devices store other values: 17 for reverb and E-Reflector parts, 1 for VDAT/VRC, 255 for DLL/PC atoms.
    The encoding is [?]; it might be `board<<4 | dsp` (17 = board 1, DSP 1), and 255 = "host".
  - `scope_device.py` reports the value as `placement` and does not enforce it.
- **`OnSameDSP` (attr 0x22) / `DSPId` (attr 0x21)**: not used by any `.dev` [V].
- Practical rule [L]: keep a device on one DSP when its cycles fit. Each cross-DSP sync wire uses a slot in the
  source DSP's sync block, and there are about 13 slots per DSP.

### 1.4 Polyphony and voice arrays [C/V]
- DSP module flag 0x400000: `VoiceDef` substitution at link time, used for example by `32ADD.dsp` and
  `6ADD.dsp`. The module also has `jmpVoice*` and `numVoices` symbols. 14 devices use such modules (mixers, 4Tap
  Chorus, Pattern Delay, Vocoders). Extras key 0x102 (`numVoices`) on the module confirms it.
- **Voice-array atoms**: the atom has fewer pads than the DSP module because one atom pad stands for a voice
  array. Examples: `M_V_M16E.dsp` (atom 9 inputs, module 24), `16MIX.dsp` (atom 2 inputs, module 17), MVC_16_MP.
  These are the MIDI Voice Control and voice-mixer modules of **every synth** (25 devices). They need the voice
  manager of base.dll/Sim2k [?]. Synths are therefore out of reach for now.

### 1.5 License atoms [C]
"Effect Package I" (`RMWMEPDA.dsp`) and "Effect Package II" (`XPN2402A.dsp`) appear in 40 of the complete devices.
At their place in the signal path, they control a gain or modulation input. Their `seg_init` does the following:

- It compares seg_mod word 9 with a constant and then clears that word.
- Only on a match does it copy the real async code (output = input) over a block of `RTS` stubs.
- Without the host's unlock word, the output stays 0. Example: Tremolo M's Volume Modulator depth input is fed
  by this atom, so the effect stays silent.

The word is computed by the board's micro-controller from the owner's key file entry and the module's
`seg_id`. This is decoded in **presets_license.md §L** and implemented in `tools/pulsar_license.py`.
`scope_device.py` flags these devices with `license_atoms` and a warning. It matches the atom name only, so 19
more devices with licensed modules (SC-EQ, SC-C, Vinco, ...) are not flagged (presets_license.md §L.6). The
recommended devices contain none.

---------------------------------------------------------------------------------------------------
## 2. Pads and wiring

### 2.1 Atom pad index [C/V]
The `ROCAtom`'s vars are its pads, in DSP descriptor order:

- inputs first (`padflags & 0xF == 1`);
- then outputs (`== 2`): async outputs first, then sync outputs.

This is exactly the numbering used by `pulsar_modules` and pulsard: `in` = index among the inputs, `out` =
index among the outputs. Unnamed DSP pads get generic names `In0`/`Out0`. `scope_device.py` checks the pad
counts and the names against `ModuleClass.pads`. A count mismatch means a voice array (§1.4).

### 2.2 Nets [C/V]
- `RODPad.link` (`RODPad::Serialize` @100b6bf0) is a reference to the pad it exposes. That can be a
  `ROCAtomPad`, a script `ROCPad`, or the `RODPad` of a child (wrapper modules).
- `RODRouting` (@100ae620) joins two `RODPad`s. Its endpoint order says nothing about the direction.
- Nets = connected components over both kinds of edge. Per net:

| net content | meaning | plan entry |
|---|---|---|
| 1 DSP output + n DSP inputs | DSP connections | `wires` (src_key, out → dst_key, in) |
| ≥ 2 DSP outputs | conflict | blocker (never happens in the corpus [V]) |
| DSP inputs, no DSP output | value from the host: a knob path (§3) or the saved pad value | `params` / `consts` |
| a pad of the root module | external device port | `ports` |

Host script pads in a net are only listeners. Only the converters and switches below move values or signals.

### 2.3 External ports [C/L]
External ports are the root's `RODPad`s whose net contains DSP pads. Direction:

- a DSP output in the net → `out`; DSP inputs only → `in`;
- otherwise use `RODPad.padbyte` [L]: 0 = drawn on the input side, 2 = on the output side. All 211 devices fit
  this rule.

An input port can feed several DSP inputs: connect the same source to every target. If the switches are in a
state where `Out` is joined to `In` (a bypassed device), the output port has no DSP source. The plan marks it
`passthrough_from`, and the client must route the input source straight on.

### 2.4 Routing switches [C]
Scripts `SignalSwitch`, `SignalSwitch2/5/Ex`, `AsyncSwitch*`, `SyncSwitch`, `BusSwitch*` and `OnOffSwitch`
rewire the device at run time with `PepAddRouting`/`PepRemoveRouting`. The script sources were decrypted as
described in §4.1:

- The general rule: pad `In<Switch+1>` is joined to `Out`.
- `SignalSwitchEx` with negative IOCount: `In` is joined to `Out<Switch+1>`.
- `OnOffSwitch`: `In` is joined to `Out` while `Switch` ≠ 0.

The saved file contains the routing that was active when the device was saved. `scope_device.py` drops that
routing and rebuilds it from the `Switch` value, so `plan(dev, dsp, switch_values={key: v})` gives the wiring for
any other position. `rewire(old, new)` then lists the needed `disconnect`/`connect` calls.

Bypass buttons, "On/Off" buttons, Mono/Stereo and similar controls are switches. Their parameter target has
`kind: "switch"`.

---------------------------------------------------------------------------------------------------
## 3. Parameters: from knob to DSP word

### 3.1 Controller value [C] (ScriptController.pep, base of NewAni, Fader, TextFader)
Pads:

- `Val`, `Min`, `Max`: integers. `Val` is the value sent to the rest of the device.
- `Curve`: 0..6.
- `Intensity`: a double, > 1 for the curves that use it.
- `Invert`, `Step`, `ModRange`, `CtrlNr`: MIDI controller number and range (ignored here).

Knob position p ∈ [0,1] (INT_MAX = 1 at the GUI level):

```
x   = Invert ? 1-p : p
Val = round(Min + curve(x)·(Max-Min))          # SendVal
p   = curve⁻¹((Val-Min)/(Max-Min)), inverted    # GetVal
```

`curve(x)` with `z = 1/Intensity`, and `x ∈ {0,1}` always linear. These are the same formulas as base.dll
`ROCParameter::GetCurveValue` @10025390 and `Value2Range::GetCurveValue` @10013d60 [C]:

| Curve | name | y = curve(x) | inverse |
|---|---|---|---|
| 0 | linear | x | – |
| 1 | exp | (I^x − 1)/(I − 1) | curve 2 |
| 2 | log | 1 + ln((1−z)·x + z)/ln I | curve 1 |
| 3 | exp, bipolar | ½ ± ½·exp-curve(\|2x−1\|) (sign of x−½) | curve 4 |
| 4 | log, bipolar | ½ ± ½·log-curve(\|2x−1\|) | curve 3 |
| 5/6 | 5-point table | linear interpolation through (0,0), 3 points from pad `CurvePtsTable` (x in MIDI units /127), (1,1); 6 = swapped axes | 6/5 |

Knobs can be stepped: `Step` > 1, or `Max−Min ≤ 10` is treated as a stepped switch. `Button`/`Button2` cycle
`Val` between `Min` and `Max`.

### 3.2 Display value [C/V]
`ScriptTextController` (with `ScriptTextEditController`, `TextEditMinMax` and `TextEditMin`) is bound to the
**same `Val` net** as the knob, through its own `Val` pad. Its pads `Min`/`Max` are the **display range**, and
`MinControl`/`MaxControl` (default 0..INT_MAX) are the range of `Val`. It has its own `Curve`/`Intensity`/`Invert`.

```
x = (Val-MinControl)/(MaxControl-MinControl)     (Invert → 1-x)
d = Min + curve_text(x)·(Max-Min)
TextEditMinMax: d = (d + Offset)/Divisor; strMin/strMax/strMid shown at the range ends / middle
text = printf(Format, d)
```

The inverse (`SetValFromText`) clamps d to [Min, Max], applies the reverse curve and scales to
MinControl..MaxControl. `TextFader*` knobs display `(Val+Offset)/Divisor` directly.

Typical patterns:

- **Curve undone by the display.** A knob with `Curve 1, I` and a text with `Curve 2, I` shows a value that is
  linear in the knob angle. Examples: Wet/Dry 0..127, and Hz on a log knob shown as Hz linear in Val.
- **Hz and ms are linear in Val.** Example: High Damp shows `Max = 24000` with `Format "%1.0fHz"`.
- **dB.** A text `Curve 2` with `Intensity = 2^31` and `Min −168, Max 18` gives a dB scale: d is a log of the
  linear gain.
- **The unit is the suffix of `Format`.** `"%1.0fHz"`, `"%1.2f ms"`, `"%1.1f dB"`, `"%1.3f s"`, `"%1.1f°"`.

Validation [V]: every text display's saved string `Str` was recomputed from its saved `Val`. 5397 of 5406 match.
The 9 misses are displays driven by other scripts (Optimaster "OFF", EDS controller modifiers).

### 3.3 Host converters on the path [C]
`scope_device.py` follows `Val` through these scripts (pep sources §4.1). Every other script on the path makes
the parameter `partial` or `unmapped_params`.

| script | in → out | transfer |
|---|---|---|
| `Long2LongSyncAtom` | LongVar → LongSyncAtomVar | identity if `noScale` (= 1, the usual case), else linear map AbsLongMin1..Max1 → AbsLongMin2..Max2 |
| `Long2LongSync` | LongVar → LongSyncVar | same, with AbsLongMin/Max → AbsLongSyncMin/Max |
| `Long2Long` | LongVar1 ↔ LongVar2 | linear map of the two Abs ranges, rounded (both directions) |
| `Double2Long`, `Float2Long` | X ↔ LongVar | linear map of the Abs ranges |
| `LongAtom2LongSyncAtom` / `LongSyncAtom2LongAtom` | – | create a child atom (`as_mult.dsp`) that is saved in the file and wired by saved routings: nothing to emulate |
| `Attenuator` | In → Out | In·Attenuator/INT_MAX·Factor, clipped |
| `Long2Flt` | Input → Output | Input/INT_MAX (float) |
| `Inverter` | InputVar → OutputVar | 1 − x |
| `ScriptAdd`, `ScriptMultiplier` | in1/in2 → out | with the other operand's saved value |
| `Dummy`, `DummySyn` | – | junction (one pad) |
| switches (§2.4) | Switch | target of kind `switch` |

Not emulated yet, so the parameter is reported in `unmapped_params`: `DelayTimeCalcEx` (delay time / BPM
sync), `If`, `Not`, `MaxOf2`, `Devider`, `FrequenzKehrwert`, `Pan`, `cut` and others.

### 3.4 Atom pad → DSP word [C]
`ROCAtom::SetInPad` @10075ce0 calls `FUN_10075b40` (the value conversion) and then Sim2k `SetInPad`. The DSP
side writes 2 words for a sync pad (module_loading.md §5). The conversion uses the pad's `Unit` attribute (0x1c)
and the ROCParam data type (1 = Long, 2 = Float):

| Unit | name (`m_unitStrings` @1013767c) | DSP word |
|---|---|---|
| 0 | – | v |
| 1 | Hz | round(v·48000/fs) (float: v/fs·48000) |
| 2 | ms | round(v·fs/48000) |
| 3 / 4 / 5 | dB0 / dB12 / dB24 (gain scales for the GUI) | v |
| 6..10 | kHz, sec, smpls, BPM, % | v (display only) |

The constant 48000.0 is `DAT_100dc258`. `UnitAttribute::sampleRate` (@10137138, initial value 48000.0) is the
current rate. The reverse direction is `FUN_1007f220`.

Consequences:

- **Unit 1 pads are phase increments.** INT_MAX = 24 kHz at 48 kHz, so `raw = F/fs·2^32`. This settles the
  `CSineR4` `f` scaling left open in module_loading.md (its NFO has `f.Unit=1`).
- **Unit 2 pads count samples**, or a fixed fraction of a delay line (e.g. MFER4 `DO`). They scale with fs.
- **Gains (Unit 3, fix 1.31)** are rate-independent.
- A unit pad driven by a host script but reached **through a DSP module** (e.g. 4-Pole's cutoff through
  `DEZIP` → `as_mult`) is not rescaled. SCOPE behaves the same way.

Encoding labels in the plan:

- `float` when the DSP pad type's low nibble is 2: IEEE single, written as its bit pattern;
- `int` for Unit 2;
- `fix31` otherwise.

All three are 32-bit words for `pulsard set`.

---------------------------------------------------------------------------------------------------
## 4. Host scripts

### 4.1 `.pep` files [C]
`App/Script/*.pep` (and `*.ped` include files) hold the scrambled source of the Lava classes. `script.dll`
`readScript` @10009280 calls pp.dll `pp_start`, whose file opener is `FUN_100045e0`. The file layout:

- bytes 0-1: magic `07 26`;
- bytes 2-17: a 16-byte key;
- the rest: the text, unscrambled by pp.dll `FUN_10004130`: byte i is XORed with `(3·i + key[i² mod 16]) & 0xff`.

The result is C-preprocessed source (`#include`, `#define`) of classes that extend `PepDisplayObject`,
`ScriptController`, `PepConvert`, ... All 891 files decode. The decoder is not part of the repository; only the
behaviour is summarized here.

Classes that are not scripts are native and live in PepBase/*.dll:

- PepBasic: `PepDisplayObject` methods such as `SetSampleRate`;
- PepNBasic: parameter, curve and interpreter helpers.

### 4.2 Do we need to emulate host logic?
- **No for audio.** Saved state plus switch emulation gives the complete DSP graph.
- **Yes for parameters.** We need the controller curve, the display formula and the converters of §3.3. In the 36 clean
  devices that gives 417 mapped parameters (51 of them `partial`); 33 panel controls stay `unmapped`.
- **Ignore:** GUI scripts (`SurfaceInterface`, `ParentTopChanger`, `PepDisplayObject`, `VUMeter`, ...) and MIDI
  controller binding (`MidiChannel`, `Ctrl_IO_7Bit`, `MidiCtrlInverter`).
- **Later:** `DelayTimeCalcEx` (all delays, but those need PC delay atoms anyway), logic scripts (`If`/`Not`)
  that drive some switches in dynamics devices, and run-time loaders (`ModuleLoader`, `PepLoad`,
  `DynamicDelay`, `EffectInserter`).

---------------------------------------------------------------------------------------------------
## 5. Defaults and presets

### 5.1 Defaults [V]
The plan's defaults come from what the file saved:

- every unwired DSP input: `consts` (value of its `ROCAtomPad`);
- every knob: `Val`.

The text check of §3.2 shows that those saved values are the device's displayed state.

### 5.2 `ROCParamList "Init"` (main list) [C/L]
`ROCParamList::Serialize` @10083320 stores a list of `{ref → pad, raw bytes}` covering the host and atom pads.
In 8713 compared int entries, 1005 differ from the saved pad values: older curves, steps or Min values, for
example. It looks like a snapshot used for "reset/init" [L], and is not used for the plan.

### 5.3 `.pre` presets [L]
The plain payload (container decoded by `scope_dev.py --plain`) starts with
"Creamware Scope technology preset file" and is read by `RODModule::LoadPresetFromFile` @10055050. It holds a
list of named presets. Each preset has records with a small index and a 32-bit value.

The values are atom pad values in the 48 kHz reference domain. For example, Phaser M's first preset holds the
same MO/F/MR words as the device. The mapping from index to pad is [?] (probably the order of the device's
preset parameter list) and is not decoded yet.

---------------------------------------------------------------------------------------------------
## 6. tools/scope_device.py

```
scope_device.py plan FILE.dev [--dsp DIR] [--rate R] [--json]   # load plan
scope_device.py calls FILE.dev --in In=n4:0 --out Out=n6:0      # pulsarctl command sequence
scope_device.py raw FILE.dev "Cutoff Frequency" 1000 --rate 44100
scope_device.py survey DIR [--jobs N] [--json OUT]              # corpus statistics
```

The module uses `scope_dev.py` as its parser and `pulsar_modules.ModuleClass` for the pad descriptors.
`--dsp` defaults to `/var/lib/snd-pulsar/dsp`.

`plan(dev_path, dsp_dir, switch_values=None)` returns:

- `name`, `file`, `complete` (no blockers), `unsupported` (blockers), `warnings`, `needs_midi`,
  `license_atoms`, `dsp_cycles`.
- `modules`: `{key, atom, dsp_file, fixed_dsp, same_dsp_group, placement, cycles}`. `key` is the module path.
- `order`: module keys ordered so that producers come before consumers.
- `wires`: `{src_key, out, dst_key, in}` (pad numbering of §2.1).
- `ports`: `{name, dir, sync, midi, target: [key, pad], targets, passthrough_from?}`.
- `consts`: `{key, in, pad, value, unit, encoding}`. Use `const_raw(c, rate)` to get the word.
- `params`, one entry per knob/fader/button that reaches a DSP pad:
  - `name`, `key`, `control`, `hidden` (outside the panel);
  - `unit` (from `Format`), `pad_unit`, `min`, `max`, `default` (display units), `display_format`, `curve`
    (a readable summary);
  - `knob` (`min`, `max`, `curve`, `intensity`, `invert`, `step`), `display` (the text controller's
    parameters), `val_min`/`val_max`/`val_default`, `discrete`, `partial`;
  - `targets`: `{key, in, pad, unit, encoding, sync, chain}`, or `{kind: "switch", key, choices, chain}`.
- `unmapped_params`: panel controls whose path uses a script that is not emulated.
- `switches`: `{key, pep, value, choices, common, selected}`.
- `display_check`: the self-test of §3.2.

Value helpers:

- `to_raw(param, display, rate)` returns the word for the first DSP target.
- `targets_raw(...)` returns all targets; a switch target yields `(key, "switch", position)`.
- `knob_to_display(param, p)` and `display_to_knob(param, d)` support SCOPE-style knobs.
- `rewire(plan_a, plan_b)` gives the wire diff after a switch changes.
- `pulsard_requests(plan, rate, inputs, outputs)` builds the JSON requests, with modules as `{"$": key}`
  placeholders that are filled from the node ids returned by `load`.

Survey result on SCOPE 5.1 (211 devices, about 45 s with 6 processes) [V]:

| result | devices |
|---|---|
| complete plan | 95 (25 need a MIDI port, 40 contain a license atom; 36 need neither) |
| no DSP module (host-only, empty Modular, External Devices) | 39 |
| PC/host atoms (PC Master/256k delays 50, Compensate Delay 7, DLL/VxD samplers/recorders) | 68 |
| voice-array pads (all synths, vocoders, samplers) | 25 |
| polyphonic modules (mixers, 4Tap Chorus, Pattern Delay, vocoders) | 14 |

The counts overlap, because one device can have several blockers.

---------------------------------------------------------------------------------------------------
## 7. Recommended first devices and how to run one

All of these are complete, mono or stereo, free of MIDI and license atoms, and use only emulated converters:

| device | modules (DSP files) | cycles | parameters (display units) |
|---|---|---|---|
| `Effects/Mono/EQ/High Cut M.dev` (Low Cut M is the same) | EQ1LP2P | 19 | Cutoff Frequency 20..20000 Hz (exp 100); Bypass (switch) |
| `Effects/Mono/Modulation/Phaser/Phaser M.dev` | MPER4, 2× DEZIP | 43 | Wet/Dry 0..127, Feedback −64..63, Rate 0.01..20 Hz, Depth 0..127, Offset 0..20 ms, Bypass |
| `Effects/Mono/Modulation/Flanger/Flanger M.dev` | MFER4, 3× DEZIP | 36 | as Phaser, plus Delay 0..20 ms |
| `Effects/Mono/Filter/4-Pole M.dev` | LP4PCR2, MLFO3, GPMOD2R5, DEZIP, AS_MULT | 91 | Cutoff 20..20000 Hz, Resonance, LFO Rate/Depth/Waveform, LFO On/Off and Bypass (switches) |
| `Effects/Stereo/Modulation/Chorus/Chorus S.dev` | MCESR4, 2× FBMIX2, 2× 2NIX, 4× FMS2A/FMA2S, 3× DEZIP, 2× MIX2 | 110 | Wet/Dry, Feedback, Rate Hz, Depth, L/R Phase °, Cross FB/Mono/Bypass switches |

Next steps after these:

- Distortion M/S and Compressor M/S. In the Compressor, the side-chain switch passes through `If`/`Not`,
  which is not emulated.
- EDS8i, the first instrument; it needs MIDI routing in pulsard.

Example: High Cut M as an insert on the left PC playback channel. In pulsard's default graph, `n4` is PC
Volume L and `n6` is Mix L, whose in0 comes from n4.

`scope_device.py calls ... --in In=n4:0 --out Out=n6:0` prints:

```
pulsarctl load EQ1LP2P.dsp name='EQ 1 2pole lowpass filter'      -> node id, say n10
pulsarctl set id=n10 in=1 value=0x6aaaaaa9                        # F = 20000 Hz (saved default) at 48 kHz
pulsarctl connect src=n10 out=0 dst=n6 in=0                       # device Out -> Mix L (replaces n4 -> n6)
pulsarctl connect src=n4 out=0 dst=n10 in=0                       # PC Volume L -> device In
```

Moving the knob to 1 kHz: `to_raw(param, 1000, 48000)` = 0x05555555 (0x05CE13BC at 44.1 kHz), then
`pulsarctl set id=n10 in=1 value=0x05555555`.

Bypass = 1: `plan(..., switch_values={"High Cut M/Switch 4#1/SignalSwitch": 1})` makes Out a passthrough of In,
so the client connects `n4 → n6 in0` again.

For Phaser M the sequence is: three loads (two DEZIP, then MPER4), two connects (DEZIP out0 → MPER4 in5/in6),
six sets, and the two port connects. Wet Level and Dry Level are written to the DEZIP inputs, not to MPER4
directly.

---------------------------------------------------------------------------------------------------
## 8. Open points
- Host atoms: PC Master/256k delays (pc_delay*.cpp in Sim2k) and the Compensate Delay Linker. These are needed by
  every delay and reverb.
- The voice manager (MIDI Voice Control, voice arrays, `VoiceDef`) for synths.
- The license atom unlock word (§1.5): done, see presets_license.md §L.
- The placement value encoding (17/255).
- The `.pre` record → pad mapping.
- `DelayTimeCalcEx` and the logic scripts used by dynamics devices.
- MIDI ports: the plan lists them (`midi: true`), but pulsard has no MIDI routing yet.

---------------------------------------------------------------------------------------------------
## 9. Dynamic devices: the factory mixers (voices, connection-driven channels, host scripts)

`Devices/Mixer/*.dev` (DynamicMixer, MicroMixer, STM 1632, STM 16 S, STM 48 S, ControlRoom, Channel) do not
instantiate everything that is in the file. How many copies of a module exist is a **voice count**, and the
mixers change voice counts at run time from host scripts. `scope_device.plan()` emulates this, and pulsard
re-plans when one of the inputs changes.

### 9.1 Voice counts [C]
- Extras 0x102 on a RODModule (`RODModule::ReadExtraChunk` @10098f00) creates attribute 0x1E `NumVoices` and
  calls `SetNumberOfVoices` (@1008e510). That function recurses into the children, but stops at a child that has
  its own attribute ≥ 0.
- `GetNumberOfVoices` (@10016b80): the nearest attribute ≥ 0, walking up the tree. If there is none, the
  global default 1 is used.
- `ROCAtom::ChangeVoices(n)` (@1008ade0) with **n = 0 frees the atom**: no DSP module exists. Inactive mixer
  channels are saved like this (`numVoices 0` on the channel group).
- **Single** atoms (module flag 0x10000) with flag 0x400000 and no voice-array pads (`32ADD`, `16ADD`) get
  `pSetVoices(module, n)`, the `jmpVoiceDef → jmpVoice<n>` patch (midi_synths.md §3.5). `32ADD` then sums
  inputs `In01..In<n>`, each times its gain pad `G<n>` (32 at most). Sync cycles = (word & 0xFFFF) +
  n·(word >> 16): `32ADD` = 9 + 2n.
- `DynVoicesOfParent.pep` sets the voice count of the module that contains it from its `Voices` pad
  (`SetNumVoices`). −1 means "inherit".
- Plan: `modules[].voices`, `set_voices: [{key, voices}]` (SetVoices for the single atoms), `dropped` (atoms
  with 0 voices; their wires, constants and parameter targets are left out). Ports always come from the whole
  device, so the port list does not depend on the voices. Parameters whose targets are all dropped stay in the
  list with `inactive: true`. A poly-capable atom (not single) with more than 1 voice would need one instance
  per voice. That is still `unsupported`, and none of the mixers has one.

### 9.2 What drives the voice counts [C/V]
The `Voices` pads are wired, through small logic scripts, to:
- **pad connections**: `RouteByContext.pep` (the pad's routing menu) sets `Connected` = 1 in
  `OnRoutingAdded`; `PadName` is the device port. DynamicMixer channel k exists while `In<k>` is connected.
  STM 1632 channel k exists while `In<k>` or `IR<k>` is connected (`Or`), and its direct-out gains while
  `D<k>`/`DR<k>` are connected. STM aux buses exist while `Aux<k>` is connected.
- **panel controls**: MicroMixer `Select Channel` (4..16) is the channel count: the master adders get n voices,
  and channel k gets `If(count > k−1)`. DynamicMixer `Select Channel` enables the group masters of channels 5-8,
  9-12 and 13-16, so it must be at least the highest channel that is used. `Stereo` / `Stereo/Mono` buttons
  (Val 1 = **mono**, 0 = stereo) remove or add the right-channel modules. `Activate Phase Compensation` /
  `Phase Compensation` adds the CompDel modules.
- Emulated logic (`Device._eval_logic`, a fixpoint over the host nets): `If` (Condition `=` `<` `>` `!` and
  two-character forms such as `>=`), `Or` (== 1), `And` (!= 0), `Not`, `MaxOf2`; `RouteByContext.Connected`.
  Routing switches and **Layers** that are fed by this logic follow the new values.
- **Layer.pep** (routing recorder, used by the mixers for mono/stereo routing and links): member pads =
  (`ModuleNum[i]` = `GetUniqueNumber()` = the `index` field of a sibling module, `PadNum[i]` = index in its pad
  list: the atom's or script's variables, else its RODPads). `LayerState` children hold `LayerRouting`
  (ModuleNum1/PadNum1/ModuleNum2/PadNum2). `OnActiveStateChanged` removes the routings among the member pads
  and restores those of the active state. `Device._layers` re-does this from `ActiveState`. It reproduces the
  saved routing of every mixer.
- `plan(..., switch_values)` keys: `"@connected": [port names]` (`RouteByContext`), `"@channels": n` (count
  net of a count-driven mixer), a knob key (override of the knob's Val, e.g. `Select Channel`, `Stereo`), a
  switch key, a Layer key. Plan output: `dynamic_ports` (the ports whose connection matters) and `structure`.
- Parameter target `{kind: "structure", key, reach}`: the knob value is a plan input. `targets_raw` returns
  `(key, "switch", Val)`, and pulsard re-plans.

### 9.3 Multi-input host scripts emulated with live state [C/V]
The channel strips compute their DSP gains on the host from several controls. In `HOST_FUNCS` they are Python
ports of the `.pep`. Their `float` variables are IEEE single precision: `pivier` and `Panf` must be float32,
otherwise L/R differ in the last 6 bits.

| pep | inputs | outputs → DSP | used by |
|---|---|---|---|
| `Pan` | Pan, Mono, Mode (0 = 3 dB sin law, 1 = linear; Mono 0 = balance) | L, R → deZipper / Attenuator factor | DynamicMixer, MicroMixer, StereoPan |
| `Attenuator` with a variable factor | In, Attenuator, Factor | Out (In·Att/0x7fffffff·Factor, clipped) | MicroMixer faders (fader · pan), MasterVerb level scalers |
| `ValToInvertGain` | Val, Invert | Out = ±Val | DynamicMixer Gain + Invert Phase |
| `Ch1632X` | Fader, Pan, Mode, Mono, Mute, Mix, AxF1-4, Pre1-4 | MixL/MixR → `Master L/R` G<k>, Asd1-4 → `Aux k` G<k> | STM 1632 |
| `SurroundpanSTM16` | PLR, PFB (−1..1), IDiv, LFEP, Fader, Mute, SoSw, L/R/Ls/Rs/C, AxF1-4, Pre1-4 | FroL/FroR/Cent/ReaL/ReaR/LFE/Asd1-4 → bus adders | STM 16 S |

Plan: `host_funcs: [{key, pep, state, outputs: {var: [targets]}}]`. A parameter target `{kind: "host",
script, var, chain}` updates one input. `host_func_update(host_funcs, script, var, value, rate)` returns the
DSP writes. `host_func_outputs(hf)` gives all of them, and pulsard writes them after a (re)build. Check: for all
138 complete devices, the 877 outputs computed from the saved inputs are equal to the saved DSP pad values.
Not emulated (left at their saved state): `SoloLogic` (Solo, Defeat, Kill Solo), `FaderGroup` /
`MuteGroupLogic` (group linking), `Diode`, `EffectInserter` (insert slots).

### 9.4 Compensate Delay Linker [C]
The atom has no `.dsp` file. In Sim2k it is a host object (`compensate_delay_link_module`, the name compare at
FUN_10c58fd0). For every sibling module named `CompDel:` (compdel8/16/32.dsp), it computes
`delay_i = max_latency − (latency_i + offset_i)` (offsets = the linker's D<n> input values, at most 16 samples)
and writes the result into the module's `delayLen` array (FUN_10c590a0 / FUN_10c59590). The input latencies come
from Sim2k's latency tracking of inserted effects. Linux has no inserts with latency, so the linker is treated as
host-only (`host_atoms` in the plan, no DSP module) and the CompDel delays stay at their loaded value (0). The
CompDel modules themselves exist only while phase compensation is on (their voices come from the Phase
Compensation button).

### 9.5 MIDI ports
The mixers have `MIn`/`MOut` (or `MIDI`) ports for controller automation. Only `M_C_*` controller modules
(`M_C_MOD` channel filter, `M_C_IO7`) use them, and those have 0 voices until a MIDI pad is connected. The plan
has `midi_ports` and `midi_optional: true`. `needs_midi` is true only if a module with a MIDI pad is not an
`M_C_*` module (synth note input). pulsard's survey keeps such devices, and the MIDI pads may stay open.

### 9.6 Parameter naming for channel strips
Parameters that drive one strip get `strip` (`Ch<n>` or `Ax<n>` for the STM 16 S aux returns), `channel` (n for
Ch) and `base_name`. The strip comes from the target module path (`…/5/…`, `…/Ch5/…`, `…/Ax3/…`), or for a bus
adder gain pad `G<n>` from what feeds `In<n>`. Duplicate names become `"<strip> <base_name>"`, for example
`Ch5 Fader` or `Ch5 Pan Position`. Other duplicates become `"<group> <index> <base_name>"`, where the index is
1 + the number of copy marks (') of the differing path component (`AuxGroup 3 Fader`, `Master 2 Mute`). Any that
are still equal get the script input name, or `#n`. `structure: true` marks a control that re-plans.

### 9.7 pulsard
- `load_device` starts a dynamic device with nothing connected. Structure and switch controls given in `params`
  go into the first plan.
- `connect` and `disconnect` on a port in `dynamic_ports` re-plan the device. During `load_project` this happens
  once, at the end.
- The re-plan diff (`_replan_diff`) unloads modules that lost their voices, after silencing their readers. It
  loads new ones on the device's DSP and applies SetVoices, the wire diff and the constants of the new modules.
  It re-links the ports, writes the host-script outputs and re-sends the parameters the user set.
- If the new modules do not fit on the device's DSP, the device is rebuilt and placed again. If the change fails,
  the previous structure is rebuilt.
- Placement (`_place`): the whole device goes on the least loaded DSP if it fits. Otherwise the module tree is
  split by sub-trees, so a mixer channel group stays together. Cross-DSP sync wires need a slot in the source
  DSP's sync block (about 11 per DSP).
- pulsard re-uses the sync slots of unloaded modules. pulsar_modules never frees them.
- Projects store `structure: {"@connected": [...]}`.
- Measured cycles at 48 kHz (budget ≈ 937 per DSP with the 25 % reserve), see the pulsard report in the
  repository history:

| device | nothing connected | typical full use |
|---|---|---|
| DynamicMixer | 120 | 536 (16 mono channels in) / 695-711 (Select Channel 16) |
| MicroMixer | 162 (4 channels) | 359-367 (16 channels, `Select Channel` 16) |
| STM 1632 | 358 | 650 (+ aux buses) / 778 (+ 16 direct outs) |
| STM 16 S | 618 | 782 |
