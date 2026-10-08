# MIDI and SCOPE synths: how Windows does it, and the Linux design

This document covers:

- How MIDI bytes travel between the PC and the DSPs on Windows (kernel MiniMidi, `sendMidiMsg`, the DSP modules
  on both ends, the word format on MIDI pads), and how the P-Plate MIDI ports work.
- How a SCOPE synth device is organised: MIDI Voice Control, voice arrays, per-voice atoms, voice mixers,
  `SetVoices`, PROCParam tables, and what runs on the host and what on the DSPs.
- The Linux design: the ALSA sequencer client (`tools/pulsar_midi.py`), voice expansion of device plans
  (`scope_device.voice_plan`), the first targets, and the steps to integrate everything into pulsard.

Prerequisites: `device_format.md` (device plans), `module_loading.md` (linker, inputs, async/sync chains),
`scScope_sys.md` (kernel driver).

Legend:

- **[C]** = read from the code. **[L]** = likely. **[?]** = guess.
- `FUN_x` / `Name @x` = function at address x. scScope.sys base 0x180000000, base.dll 0x10000000,
  Sim2k.dll 0x10c00000. DSP offsets are seg_mod word offsets of the module (`DM(n,I0)`).

No SCOPE file contents are reproduced. Listings show addresses, mnemonics and comments only.

---------------------------------------------------------------------------------------------------
## 0. TL;DR

1. **One MIDI message = one 32-bit word** `status<<16 | data1<<8 | data2` [C]. On a MIDI pad (pad type low nibble
   0xE), a new message is a *change* of the output word. The sender toggles bit 31 on every message, so that two
   equal messages in a row still differ. Receivers compare the input with the last word they saw, once per
   async pass.
2. **PC → DSP on Windows** [C]: the host atom "MIDI Output Device" (Sim2k `midiOut_module`) feeds the DSP module
   **SNC2MIDI.dsp** ("Sync to MIDI"). The kernel writes each word into a 32-entry FIFO in DSP memory and then
   writes the new write position (`FUN_1800184d0`). SNC2MIDI pops one word every second async pass and sends it
   with the bit-31 toggle. There is also a sample-timestamped path through a PC slot (§1.3), which this device
   does not use.
3. **MIDI Voice Control is a DSP module** [C] (M_V_M16E, M_V_M192, MVC_16_MP ...). It does all of the
   following on the DSP:
   - MIDI parsing and channel filtering;
   - voice allocation and stealing;
   - pitch → phase increment, pitch bend, RPN, sustain;
   - per-voice gate, velocity and aftertouch outputs.

   The host only instantiates the voices and uploads the tables.
4. **Voices** [C]:
   - Atoms with module flag 0x10000 (or the atom extras chunk 0x108) are *single* atoms. One DSP module serves
     all voices through pad arrays (records start at pads with type bit 0x2000). The host calls
     `SetVoices(n)`, which writes `numVoices` and repatches a `…VoiceDef` jump.
   - All other atoms are *poly-capable*: they get one DSP module instance per voice.
5. **Every factory synth contains exactly one copy-protection atom** (symbol `magicProt`: EDS 16i, Poison FM,
   Synthesizer Package I/II, ...). It sits in the MIDI path or on the master gain, so the synth stays silent
   until the host unlocks it. This is not analysed and not bypassed here (§4).
6. **Linux** (§5):
   - `pulsar_midi.py` is an ALSA sequencer client "Pulsar2 MIDI" that converts events to SCOPE words and writes
     them into SNC2MIDI's FIFO with two SetValue frames per message. It is tested offline, including an ALSA
     loopback.
   - `scope_device.voice_plan()` expands voice arrays and per-voice atoms into a plain pulsard plan, with
     `set_voices` and `tables` lists.
   - First audible target: a hand-built 4-voice synth from unprotected SCOPE modules
     (`pulsar_midi.py ops`).
   - First factory devices once unlocking exists: **EDS16i V2** (drums, no voice arrays), then **Poison**
     (polyphonic).

---------------------------------------------------------------------------------------------------
## 1. MIDI on Windows

### 1.1 Word format [C]
- **Render, PC → card**: `MiniMidi.cpp` `modWriteLongBufferData` (`FUN_18000bf40`) parses the byte stream:
  running status, realtime bytes inside messages, and SysEx. It calls `FUN_18000bed0`, which byte-swaps the
  little-endian message (`b0 | b1<<8 | b2<<16`) into `b0<<16 | b1<<8 | b2` and calls
  `sendMidiMsg = FUN_1800186e0(card 0, stream, word, slot type 6)`.
- **Message types**:
  - Channel messages and F1/F2/F3 become one word each.
  - Realtime F8..FF becomes `byte<<16`.
  - SysEx is sent in chunks `F0 d1 d2`. The last chunk carries `F7` in the first free byte: `F0 d F7` or
    `F0 F7 00`.
- **Capture, card → PC**: `FUN_18000c4a0` is the exact inverse. It reads words from the MIDI-in slot (type 5) at
  stride 2 (word + one more word, a timestamp [?]) and rebuilds the bytes, including the F0 … F7 framing. The
  DPC polls the MIDI-in streams with `FUN_18000c990` after every block.
- **DSP side** [C]: M_V_M16E `MIDI_SCHL` reads the input word and takes the following fields:
  - `FEXT 20:3` = status high nibble − 8, used as a jump table: note off, note on, poly AT, CC, PC, ch AT,
    pitch bend, system;
  - `FEXT 16:4` = channel;
  - `FEXT 8:7` and `FEXT 0:7` = data bytes.

  It processes the word only if it differs from the last word (seg_mod 0x155). SNC2MIDI, Midi2snc and pc2midi
  all toggle bit 31 (`BTGL … BY 31`) when they emit.

### 1.2 PC → DSP: `sendMidiMsg` (`FUN_1800186e0`) [C]
The stream descriptor comes from the host atom (Sim2k `midiOut_module`, constructor `FUN_10c5d3c0`):

| field | default | meaning |
|---|---|---|
| +0xde | −1 | DSP address of the FIFO array; set when the atom's "MIDI Fifo" output is connected |
| +0xe2 | 0 | host write index |
| +0xe6 | 0x20 | FIFO size (32) |
| +0xea | −1 | DSP address of a mailbox word (atom output "MIDI Output" connected directly to a MIDI pad) |
| +0xee | 0 | bit-31 toggle for the mailbox mode |
| +0xf6 | −1 | BAR word index where the DSP mirrors the FIFO read position ("MIDI Fifo read position" input) |
| +0xfe | −1 | DSP address of the write-position word ("MIDI Fifo write position" output) |

The pad names come from `FUN_10c5d220` / `FUN_10c5d2e0`. Disconnecting a pad resets its field to −1
(`FUN_10c5d1b0`).

The kernel picks one of four modes:

- **FIFO mode** (`+0xde ≥ 0`, `+0xf6/+0xfe` set, `FUN_1800184d0`). This is what `Sequencer MIDI Source.mdl` uses:
  all of Fifo, FPwr and FPrd are routed.
  ```
  if ((wr + 1) & (size-1)) == BAR[f6]: full -> queue the word in a host ring, retried later
  SetValue(fifo + wr, word); wr = (wr+1) % size; SetValue(fpwr, wr)
  ```
- **Mailbox mode** (`+0xea ≥ 0`). The kernel busy-waits until at least `ctx+0x3a8c` samples have passed since
  the last message (bounded by about 5 ms of `KeQueryPerformanceCounter`). It then does
  `SetValue(+0xea, word | toggle)` and flips the toggle. This is the "sendMidi1 (tm, dtm …)" log line.
- **Slot mode** (all fields −1: no FIFO, no mailbox). The word is written straight into the DMA ring of the
  type-6 slot, at a sample position inside the next block. Successive words are spaced by `ctx+0x3a8c` samples
  ("sendMidi1/2/3 (tm, spos, clk, off, msg)"). This path is sample-timestamped. On the DSP, SNC2MIDI's sync
  input "Sync In" sees the word in that sample and latches every non-zero value.
- **Host queue** ("fill buffer"): used when none of the above can take the word right now.

### 1.3 SNC2MIDI.dsp ("Sync to MIDI") [C]
Pads:

| pad | kind | type | use |
|---|---|---|---|
| in0 Sync In | sync | | slot mode input |
| in1 MIDI Fifo | async | 0x20000E | PROCParam: value word = FIFO address |
| in2 MIDI In | async | MIDI | passthrough input |
| in3 MIDI Fifo Write Position | async | | write position |
| out0 MIDI Out | async | MIDI | the MIDI output |
| out1 MIDI Fifo Read Position | async | | read position |

The async code picks its source in this order:

1. If in2 is connected (its input word ≠ `_null`): passthrough from that MIDI pad.
2. Else, if the FIFO address (`DM(DM(in1))`) is non-zero, FIFO mode:
   - A countdown at seg_mod 0x10 makes it pop at most one word every second async pass. After each pop it is
     reloaded from M14 = 1.
   - If `rd == DM(DM(in3))` the FIFO is empty.
   - Otherwise it pops `fifo[rd]`, sets `rd = (rd+1) & 31`, stores rd in out1 (seg_mod 7) and exports it.
3. Else: the last non-zero sample latched by the sync part.

Every word goes out through `sendR0`: `out0 = word | toggle; toggle ^= 0x80000000`, and the word is exported to
other DSPs (`os_sendmsgPX2`).

Rates:

- The async chain runs once every `asRatio` word clocks (`dsp_boot_analysis.md` §4.1; asRatio = 15 at 48 kHz),
  so about 3200 passes/s.
- SNC2MIDI therefore delivers up to about **1600 messages/s** [L]. That is faster than a MIDI cable
  (about 1000/s).
- Receivers such as MVC read once per pass, so they never miss a message coming from SNC2MIDI. That is why
  SCOPE always puts SNC2MIDI between the host and the synths.

### 1.4 DSP → PC [C/L]
"Sequencer MIDI Dest.mdl" = host atom "MIDI Input Device" (Sim2k) + **Midi2snc.dsp** ("MIDI to Sync"):

- Async part: on a change of its MIDI input, the module stores the word in its own FIFO and exports it.
- Sync part: outputs the word (bit 31 cleared) for exactly one sample on "Sync Out", and 0 otherwise. This output
  feeds a capture slot (type 5).
- The kernel decodes the slot with `FUN_18000c4a0`.

Linux (later): connect Midi2snc's Sync Out to a third capture slot and scan the ring for non-zero words, or poll
its FIFO position.

### 1.5 Other MIDI modules
These modules are all ordinary async modules with MIDI pads:

- `pc2midi` (with its own 128-word FIFO);
- `M_C_MOD` (MIDI channel filter);
- `M_C_IO7` (controller → value, 120 outputs);
- MIDI mergers, splitters and filters (`Devices/Midi/*.dev`, complete plans).

---------------------------------------------------------------------------------------------------
## 2. P-Plate MIDI ports [C/L]

| device (.io) | DSP module | DSP | library | how it works |
|---|---|---|---|---|
| P-Plate MIDI A Source | P2MID_IN.dsp "P2 MIDI Input" (0 in, 1 MIDI out) | 0 (fixed, flags 0x21030000, one per board) | P2MID_IO.ol, MIDI_IN.ol | P2MID_IO's seg_init hooks the **SPORT0 receive interrupt of DSP0** (`spr0_svc`), the plate's serial link. The ISR assembles the incoming bytes (`P2MidiMsgInput`, `P2newwordflag`). P2MID_IN's async code copies each new word to its MIDI output and exports it |
| P-Plate MIDI A Dest | P2_MOU.dsp "P-Plate Midi Dest" (1 MIDI in) | 0 (fixed) | PULSMOUT.ol | Serialises the words back to bytes, including running status and SysEx (`Seriell_FIFO`). It writes them to `pio_midiout`, paced by `wclk` |

Both link offline with `pulsar_modules` on DSP0. No host and no µC (DSP5) are involved [L].

To use the hardware MIDI port on Linux, load P2MID_IN on DSP0 and connect its output (async out 0) to a synth's
MIDI input. Cross-DSP async export already works. No host code is needed.

---------------------------------------------------------------------------------------------------
## 3. How a SCOPE synth is organised

### 3.1 Signal flow (EZSynth, Poison, miniscope …) [C from the plans]
```
device MIDI port -> [protection atom] -> (MIDI Channel Filter) -> MIDI Voice Control (single atom, n voices)
   MVC per voice:  Note, Velocity, Aftertouch, Gate (async)  +  Frequency (sync, phase increment)
   per-voice atoms (n instances): oscillators, filters, ADSRs, VCAs, key-follow tables ...
   ADSR "envelope sync output" -> MVC "Envelop Generator Sync Input" array (voice free / stealing)
   per-voice audio -> Mixer 16 (single atom, array input) -> mono FX (chorus, delay) -> device outputs
M_C_IO7 (controller filter) turns CCs into values for knobs (host binding, ignored for now)
```

### 3.2 MIDI Voice Control is DSP code [C]
- M_V_M16E ("MVC Easy 16") has handlers `NOTE_ON`, `NOTE_OFF`, `RETRIGGER`, `SUSTAIN`, `Pitch_Wheel`, `RPN`,
  `ALL_NOTES_OFF`, `PROG_CH` and `CHAN_AFTER`.
- It allocates voices itself. The per-voice tables live at seg_mod 0xB7 (tune), 0xD7 (age), 0xF7 (note) and
  0x107 (velocity), 16 entries each.
- It turns note + tune-table offset + coarse/fine/pitch bend into a phase increment, using `Pow2` (Mathb0.ol) and
  `FREQUENZ_TAB2` (Tunedef.ol).
- Inputs:
  - `Tune Tab` (type 0x800001, PROCParam, 128 signed entries): the module reads `DM(DM(in1)) + note`.
    **It must point at a valid table.** An unconnected input means `_null`, which makes the table base 0 and
    reads IOP registers. All zeros = equal temperament [L].
  - `Velocity Tab` and `Aftertouch Tab`: if the host does not write them, the module uses its own tables
    `VelTab` / `AtTab` (seg_mod relocations of the input words).
  - `Channel Number`: 0..15, or 16 (bit 4) = omni.
- Outputs:
  - `Frequency Output` is a sync phase increment in the 48 kHz unit (2^32 = fs). Whether the module rescales for
    44.1 kHz is [?]. Use 48 kHz, the pulsard default.
  - `Gate`, `Note`, `Velocity` and `Aftertouch` are async, per voice.

### 3.3 Voice arrays [C] (base.dll `ROCAtom::LoadAtom` @100bb3d0)
- base.dll scans the module's pads in order:
  - Pad type bit **0x2000** marks the first pad of a voice record.
  - The distance to the next marker is the record length.
  - Array length = (pads from the first marker to the end) / record length.
  - Inputs, async outputs and sync outputs are scanned separately; all three must give the same voice count, or
    base.dll logs "Polyphonic atom … different number of … voices".
- The atom shows only the first record. That is why device atoms have fewer pads than the `.dsp`, the old
  "pad count differs" blocker.
- Sim2k marks such modules 0x400000 internally, and `maxVoices` = array length.
- `ROCAtom::VoiceModuleIn` @100532e0 / `VoiceModuleOut` @100533e0 map atom pad `p`, voice `v` → module pad:
  `p` if the pad is before the array, `p + v·rec` inside the array, `p + (n−1)·rec` after it. Sync outputs are
  mapped the same way after the async outputs. `scope_device.voice_in_pad` / `voice_out_pad` implement this.

Examples:

| module | inputs | async outs | sync outs | n |
|---|---|---|---|---|
| M_V_M16E | 8 globals + EG-sync array (rec 1) | 4 globals + [Note, Vel, AT, Gate] (rec 4) | Frequency (rec 1) | 16 |
| MVC_16_MP | 16 globals + EG sync (rec 1) | 2 globals + rec 5 | [Osc freq, pitch track] rec 2 | 16 |
| 16MIX | Master Gain + Audio In array (rec 1) | – | 1 global | 16 |

### 3.4 Single atoms and per-voice atoms [C]
- `ROCAtom` field +0x2c = module flags, +0x30 = voice count n, +0x44 = voices per instance.
- **Single** atom: module flag **0x10000**, or the atom carries extras chunk **0x108** (`ReadExtraChunk` sets
  +0x40, and `ForceSingleVoice` @1008b030 ORs 0x10000 into the flags).
  - One DSP instance.
  - `pSetVoices(module, n)` when n changes.
  - Pads outside the arrays are mono.
- **Poly-capable** atom (flag clear, no 0x108): `ChangeVoices` @1008ade0 creates one instance per voice
  (`InitWithParameters(this, count, offset)`). The array +0x28 holds the instances.
- n comes from `RODModule::GetNumberOfVoices` @10016b80: attribute 0x1E `NumVoices` found walking up the tree,
  else the global `CSet/voices` (default 1, `ReadNumVoices` @10014340).
- Synth panels change n at runtime through `PepDisplayObject::SetNumVoices` → `RODModule::UpdateNumberOfVoices`
  @1008e5e0 → `SetNumberOfVoices` (recursive) → `ChangeVoices`. None of the shipped `.dev` files stores
  attribute 0x1E [V on EZSynth].
- Routing, `ROCAtom::RouteInput` @10053580:
  - For every destination voice u < `VoicesIn(dst, pad)`, connect source voice u.
  - A *single* source output (`SingleOut` ≠ 0) feeds every destination voice (fan-out).
  - A mono input (a single atom's global pad) gets only voice 0.
  - SCOPE therefore also instantiates n copies of poly-capable atoms that sit after the voice mixer (EZSynth's
    Amp + Dist and Mix1). Only voice 0 of those reaches the output; the other copies compute the same thing.
    Linux skips them (§5.2).

### 3.5 SetVoices on the DSP (Sim2k `SetVoices` @10c06a00 → `FUN_10c17de0`) [C]
For modules with 0x400000 and n ≠ the current count (clamped to `maxVoices`):

1. `SetValue(seg_mod + numVoices, n)`.
2. Find the seg_sync relocation whose symbol contains `VoiceDef` (`jmpVoiceDef` in 16MIX/32ADD,
   `copySyncOutVoiceDef` in M_V_M16E).
3. Replace the last three characters ("Def") with the decimal n. Re-point the instruction at that symbol, the
   entry into the unrolled per-voice code: `jmpVoice4`, `copySyncOutVoice4`.

On Linux this is a `set` op plus a `patch` op (sysmsg 6), the same mechanism as the `ret_sync` chain patches:
`pulsar_midi.set_voices_ops()`.

### 3.6 PROCParam tables [C]
- Inputs whose pad type has bits 0x00F00000 are table inputs:
  - 0x800000: 128-entry tune, velocity and key tables;
  - 0x200000 / 0x100000: FIFOs and arrays.
- The module dereferences them twice. The input word points at a value word, and that value word holds the
  table's DM address.
- The device stores the table contents in the atom pad (`PROCParam`, `DataElements`, items). Key-follow tables
  (`MIDI2FP` "TIn") hold real data, and the MVC Tune table holds zeros.
- base.dll uploads them when the atom is created. Host scripts (`MIDIVelocityTab.pep` …) rewrite them when the
  user edits a curve.

### 3.7 Host or DSP?

| function | where |
|---|---|
| MIDI parsing, channel filter, voice allocation/stealing, note → frequency, pitch bend, sustain, gate/velocity | DSP (MVC, M_C_MOD) |
| CC → parameter values (`M_C_IO7`) | DSP (outputs), host binding to knobs (MidiChannel/Ctrl_IO_7Bit scripts) |
| Voice instantiation, SetVoices, table upload | host (base.dll/Sim2k) → **Linux: pulsard / scope_device** |
| MIDI delivery into the DSP FIFO | host kernel → **Linux: pulsar_midi.py** |
| Copy-protection unlock | host → **not available on Linux** |

---------------------------------------------------------------------------------------------------
## 4. Copy-protection atoms (blocker for every factory synth) [C]

- `scope_device.protected_atoms()` finds modules with the `magicProt` symbol. 179 `.dsp` files carry it.
- Each factory synth device contains **exactly one**:

  | device | atom | what it gates |
  |---|---|---|
  | EDS16i V2 | DEVM01M "EDS 16i" | MIDI input of the whole drum kit |
  | Poison | LROCCA "Poison FM" | master gain of the voice mixer |
  | EZSynth, BlueSynth, miniscope, U KNOW 007, EDS8i | KLNUMJMP* "Synthesizer Package I" | MIDI or a gain |
  | Inferno, Prisma, Arpeg01 | "Synthesizer Package II" variants | |
  | Arpeg02 | SFSSV8A | |
  | SB404 V2 | MLTISWQA | |

- seg_init compares the host-written `magicProt` word with a value. Only on a match does it copy the real code
  over `RTS` stubs (`device_format.md` §1.5).
- The host derives the word from the user's license (extras 0x105 `moduleKey` [?]).
- This work does **not** analyse, reproduce or bypass that mechanism. Factory synths therefore stay silent on
  Linux until a legitimate unlock path exists, for example with the owner's license data. The project owner must
  decide that. Everything else below works without it.
- `voice_plan()` reports the atom under `protected_atoms`.

---------------------------------------------------------------------------------------------------
## 5. Linux design

### 5.1 (a) ALSA sequencer client "Pulsar2 MIDI" (`tools/pulsar_midi.py`)
- **Client**:
  - `SeqClient` opens `default` (duplex, non-blocking), names the client "Pulsar2 MIDI" and creates a writable
    port "Synth In" (WRITE|SUBS_WRITE, MIDI_GENERIC|SYNTH|HARDWARE|SYNTHESIZER).
  - It decodes every event back to bytes with `snd_midi_event_decode` (running status off) and feeds them to
    `MidiPacker`.
  - It uses no queue, so delivery is immediate. Ardour, PipeWire's MIDI bridge, keyboards, `aconnect` and
    `aseqdump` all reach it.
- **Packer**: `MidiPacker` produces exactly the words of §1.1 (realtime bytes, running status, SysEx chunks).
  `unpack()` is the capture-side inverse.
- **DSP side**:
  - `midi_source_ops(rack, dsp)` loads SNC2MIDI on the DSP that hosts the synth's MIDI consumer (any DSP works;
    cross-DSP async export is supported).
  - It allocates a 32-word DM FIFO, points in1 at a value word holding the FIFO address, and gives in3 a value
    word for the write position.
  - It leaves in0/in2 at `_null`, which selects FIFO mode.
  - It returns `{dsp, fifo, wpos, rpos}`, where rpos = seg_mod + 7 = out1.
- **Delivery**: `ScopeMidiFifo.send(words)` sends per message the two frames below. Both go through
  `Board.set_value` → `PULSAR_IOCTL_SEND_MSG` under pulsard's board lock:
  ```
  SetValue(dsp, fifo + wr, word)          header = 1<<25 | dsp<<21 | (fifo+wr), payload word (bit 31 clear)
  SetValue(dsp, wpos, (wr+1) & 31)        header = 1<<25 | dsp<<21 | wpos
  ```
  Each frame is wrapped as usual: `[T|0x120000, T|0x120000, hdr|0x20000000, value, T|0x20100000, 0x0FE0C008,
  7 pad, 0]`.
- **Flow control**:
  - The sender estimates the fill level with a conservative drain rate of 1000 words/s.
  - When the FIFO looks full, it reads the real read position with `get_value(dsp, rpos)` (sysmsg 8).
  - After 50 ms it drops the word and counts it, so the sequencer thread never blocks.
  - The FIFO holds 31 words, so a chord of 31 notes goes through at once.
- **Latency and jitter**: about 0.3–0.6 ms (async pass) plus the ioctl. There are no sample-accurate timestamps.
  Slot mode (§1.2) would provide them later: a third playback slot whose ring carries MIDI words at sample
  positions into SNC2MIDI's Sync In.
- **Tests** (offline, no root, no hardware):
  - `pulsar_midi.py selftest`: packer cases, FIFO wrap/full/drop, and ALSA loopback (own sender client →
    "Pulsar2 MIDI selftest", events → words).
  - `pulsar_midi.py listen` prints the words for any connected source.
  - `pulsar_midi.py ops` prints the linker ops of the test synth (§5.3).

### 5.2 (b) Voice expansion of device plans (`scope_device.voice_plan(dev, voices, dsp_dir)`)
1. Load `plan()`, then classify each module:
   - **array**: single atom (0x10000 or extras 0x108) whose pads contain 0x2000 records;
   - **single**: single atom without arrays;
   - **poly**: everything else.
2. Remove the "pad count differs" blocker when the atom's pad counts match `voice_layout()`. Clamp n to the
   smallest array.
3. Find the voice-dependent poly atoms: the fixpoint of "fed by an array output or by another voice-dependent
   atom". These are instantiated n times (`key`, `key#v1` … with `voice` / `voice_of`). Other poly atoms stay
   single, which gives the same audio as SCOPE (§3.4). `scope_exact=True` replicates all of them.
4. Rewrite wires, consts, param targets and port targets to **module pad numbers** per voice, following the
   `RouteInput` rules. The result plugs into `pulsard_requests()` and pulsard's device loader unchanged.
5. Add the extra lists:
   - `set_voices` [{key, voices}];
   - `tables` [{key, in, pad, elements, values, default_symbol, keep_default}];
   - `protected_atoms`.

   Array module cycles = low 16 bits + high 16 bits × n (16MIX 0x20008, MVC 0xA0013) [L].
6. `pulsar_midi.py voices FILE.dev --voices N` prints the result. Examples:
   - EZSynth, 4 voices: 34 modules, 670 cycles.
   - Poison, 4 voices: 195 modules, 1935 cycles, 3 SetVoices, 35 tables.
   - miniscope, 6 voices: 112 modules.

   With the protection atom excluded, all three expand without a blocker.

### 5.3 (c) First targets
**1. Hand-built test synth (works without any unlock).** `pulsar_midi.test_synth_ops(rack, dsp=2, voices=4)`
uses SCOPE DSP modules only, all unprotected, all on one DSP. That gives no sync slots, about 300 cycles and 16
modules.
```
SNC2MIDI (FIFO 32 words)  --MIDI-->  M_V_M16E "MVC Easy 16"  (SetVoices 4, Tune table = 128 zeros, channel 16 = omni)
   for v in 0..3:  MVC out 68+v (Frequency v) -> MMOSC6.f        MMOSC6 Sel = 4 (as EZSynth)
                   MVC out 7+4v (Gate v)      -> ADSR-EG5.gate   ADSR A 0, D max, S 0, R 4800 (100 ms), slope 5
                   ADSR out0 (EG sync)        -> MVC in 8+v      ADSR out1 (level) -> LINVOL.vol
                   MMOSC6 -> LINVOL -> 16MIX in 1+v              (16MIX SetVoices 4, master gain 1/4)
16MIX -> LINVOL "Synth Out" (0.1 = -20 dB safety) -> pulsard Mix L / Mix R (n6/n9 in1)
```
Hardware check:

1. Note-on → audible saw.
2. Chords → up to 4 notes.
3. Note-off → release.
4. Pitch bend works.

If there is no sound, read back these words with `get_value`: MVC note/gate (seg_mod +4+4v / +7+4v),
`numVoices`, and SNC2MIDI rd (seg_mod 7) == host wr.

**2. First factory device: `Synths/EDS16i V2.dev`** (drum synth):

- Complete plan: 182 modules, 2036 cycles, 16 × MDVC1E (MIDI Drum Voice Control, mono).
- No voice arrays, no host atoms, nothing else needed.
- Blocked **only** by its protection atom DEVM01M, which carries the MIDI input.

**3. First polyphonic factory synth: `Synths/Poison.dev`**:

- Complete after `voice_plan` (MVC 162 + 2 × Mixer 16).
- No PC delay atoms. Its chorus is MCESR4 (DSP).
- Protected by LROCCA on the master gain.

### 5.4 Integration into pulsard (not done here; pulsard.py untouched)
1. **MIDI node**:
   - New command `midi_source {dsp}`: run `midi_source_ops()` under the board lock and add a node "pc_midi"
     (1 MIDI output, async out 0).
   - Start `MidiBridge(ScopeMidiFifo(board, info, lock=self.lock))` in a daemon thread. The client name is
     "Pulsar2 MIDI".
   - On unload/reset: stop the bridge, unload the module, call `free_tables()`, and `fifo.reset()` after a
     reload.
2. **MIDI wiring**: allow connections between MIDI pads (type 0xE). This is an ordinary async connect; across
   DSPs it is an export. A device's MIDI input port targets the MIDI pad(s) listed in `ports`.
3. **Devices**:
   - In `load_device`, use `scope_device.voice_plan()` when the plan has voice arrays. The node gets a "voices"
     parameter that rebuilds the device with a new n.
   - Apply `set_voices` with `set_voices_ops()` after loading.
   - Apply `tables` with `table_ops()`. Skip entries with `keep_default`.
   - Keep `needs_midi` devices out of the "usable" list only while they carry `protected_atoms`.
4. **Placement**:
   - Keep each voice (and ideally all voices + MVC + mixer) on one DSP. Every cross-DSP sync wire uses one of
     about 13 sync slots of the source DSP.
   - Above one DSP, put whole voices on a second DSP (base.dll has the "OnSameDspPerVoice" option for the same
     reason).
5. **Packaging**:
   - `pulsar_midi.py` goes to /usr/lib/snd-pulsar/tools.
   - The ALSA client lives inside pulsard (root). `/dev/snd/seq` is accessible to root, and PipeWire's
     ALSA-seq bridge shows it to Ardour as "Pulsar2 MIDI: Synth In".
6. **P-Plate MIDI**: catalog entries P2MID_IN / P2_MOU (fixed DSP0) can be wired like any module.
   No host code needed.

---------------------------------------------------------------------------------------------------
## 6. Open points
- Unlocking protection atoms (owner's decision, §4).
- MVC frequency at 44.1 kHz (rescaled or 48 kHz-referenced?) [?]. Test the pitch of A4 at both rates.
- Tune table semantics. Zero = equal temperament [L]. Verify with note 69 → 440 Hz.
- DSP → PC MIDI (Midi2snc + capture slot) and sample-timestamped PC → DSP (slot mode).
- Velocity/aftertouch tables written by host scripts (`MIDIVelocityTab*.pep`). For now the device-stored values
  are used, or the module defaults when the stored values are zero.
- CC → knob binding (`MidiChannel`, `Ctrl_IO_7Bit`) for device parameters.
