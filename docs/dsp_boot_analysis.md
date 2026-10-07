# Pulsar II DSP boot / OS analysis (puls2os0..5.21k), and why no DSP acknowledges

Tool: `tools/sharc_dis.py` (ADSP-2106x/21065L disassembler; a port of MAME `sharc_dasm.cpp` (BSD-3) plus COFF
symbols, the 21065L IOP register names, and IOP value annotation). Listings below come from
`sharc_dis.py scope_full/app/App/Dsp/puls2os0.21k`, with any comments added after `;;`.
Chip facts are from the *ADSP-21065L SHARC Technical Reference / User's Manual* (Appendix E IOP map,
Appendix F vectors, ch. 8 host interface, ch. 12 booting). The PDFs were fetched from smd.hu and are not
kept in the repo.

Legend: **[V]** = verified, read directly from the DSP code, the 21065L manual or the Sim2k binary.
**[L]** = likely. **[?]** = guess.

---------------------------------------------------------------------------------------------------
## 0. TL;DR: two definite host-side bugs, both caused by using the wrong DSP class in Sim2k

`re_notes/boot_sequence.md` §3.4 and `pulsar_loader.py` take their DSP memory layout from the Sim2k class
`sharc` (vtable 0x10c8a4ac): pm 0x20000, dm 0x25000/0x3000, sysmsg 0x24200. **Pulsar2 does not use that
class.** The Pulsar2 board's DSP factory (board vt+0x168 = `FUN_10c32880`) normally (cfg fields <0)
constructs `FUN_10c3c100`, class **`pluto`** (the 21065L class, vtable **0x10c8a634**). Its layout slots are [V]:

| vt slot | sharc (wrong) | **pluto (Pulsar2)** | function |
|---|---|---|---|
| +0xd8 pmstart | 0x20000 | **0x8000** | FUN_10c38360 |
| +0xdc pmsize | 0x2000 | **0x1800** | FUN_10c38390 |
| +0xe0 dmstart | 0x25000 | **0xC400** | FUN_10c383c0 |
| +0xe4 dmsize | 0x3000 | **0xE000 - dmstart = 0x1C00** | FUN_10c383f0 |
| +0xf4 sysmsg base | 0x24200 | **symbol `sysMsg` (= 0xC419)** | FUN_10c384b0 |
| +0xf8 comm base | – | 0xC080 | FUN_10c39fc0 |
| +0xfc comm size | – | 0x380 | FUN_10c38430 |
| +0x104 coreClock | 40 MHz | 60 MHz | FUN_10c38460 |
| +0xf0 | sysmsg 4 | no-op | thunk_FUN_10c40550 |

Consequences, both visible in `re_notes/sram_after_linux_boot.txt`:

1. **The DSPs never leave the boot loader.** The ROM-boot code at 0x8080 (`wait_boot`) spins in `wait_loop`
   until **DM 0xDFFF != 0**. Sim2k's Run step (`FUN_10c20990` → dsp vt+0xd4 `FUN_10c3aa50`) writes the
   word `1` at `dmstart + dmsize - 1`. With pluto that address is **0xC400 + 0x1C00 - 1 = 0xDFFF**, which is
   exactly the release flag. Our loader writes it to **0x27FFF** instead (FIFO frame `22027fff 00000001`
   at BAR+0x81a98 etc.). 0x27FFF is not internal memory on a 21065L, and the address also collides with
   header bit 17. So no DSP ever reaches `os_start`, no DSP computes `dspAckDest`, and no sysmsg is ever
   polled.
2. **The sysmsg is written to the wrong place, in the wrong order.** The OS polls **DM 0xC41C
   (`sysMsg+3`) as the message type** and reads its parameters from 0xC41A/0xC41B. Sim2k `FUN_10c33410`
   (asm @10c33485..10c334b7) builds the 3-word block as **{a, b, type}** and uploads it to
   `vt+0xf4() + 1` = **0xC41A**. Our loader sends `[type, a, b]` to 0x24201 (`26024201 00000008 0000c409
   00000000`). Even with fix 1 applied, the OS would read type = b = 0, hit `IF LE RTS` at 0x81D5 and
   never answer.

Fix (in `tools/pulsar_loader.py`):
```python
DM_START, DM_SIZE = 0xC400, 0x1C00          # pluto (21065L) layout -> stack/release word at 0xDFFF
...
self.upload_data(dsp, [1], DM_START + DM_SIZE - 1)                  # = 0xDFFF   (Run, per DSP)
...
def sysmsg(self, dsp, mtype, a, b):
    sysmsg_base = self.syms[dsp]["sysMsg"]                           # 0xC419 in all six images
    self.upload_data(dsp, [a, b, mtype], sysmsg_base + 1)           # {a, b, type}: type lands last, at +3
```
`pmstart/pmsize` (0x8000/0x1800) also matter later, for module placement.

---------------------------------------------------------------------------------------------------
## 1. 21065L facts used here [V, manual]

* Memory map: IOP regs 0x00..0xFF; block 0 normal words 0x8000..0x9FFF (PM code, IVT at 0x8000); block 1
  normal words 0xC000..0xDFFF (DM); 0x20000+ = external memory (bank 0). On this card,
  0x20000 is the Cebulon/host side of each DSP's external bus.
* IOP registers written by the host: **0x1C = DMAC0** (DMA channel 8 = EPB0 control), **0x40 IIEP0, 0x41 IMEP0,
  0x42 CEP0**, 0x45 EIEP0, 0x46 EMEP0, 0x47 ECEP0. Ch. 9 (EPB1): 0x1D DMAC1, 0x48..0x4F. Others used by the OS:
  0x00 SYSCON, 0x02 WAIT, 0x0D MSGR5, 0x28/29/2A TPERIOD0/TPWIDTH0/TCOUNT0, 0x2E IOCTL, 0x2F IOSTAT.
  (SDRDIV is 0x20. The OS never writes it, and IOCTL=0xC00 = DSDCTL|DSDCK1 means **no SDRAM is used**.)
* DMACx bits: 0 DEN, 1 CHEN, 2 TRAN (1 = internal→external), 5 DTYPE (1 = 48-bit instructions), 6-7 PMODE
  (with HBW=32-bit: 01 = 32-bit words, no packing; 10/11 = 32↔48 packing), 8 MSWF, 9 MASTER, 10 HSHAKE, 11 INTIO,
  12 EXTERN, 13 FLSH. So the host values decode as:
  `0x20e0` = FLSH + DTYPE + PMODE 11 (flush, set up 48-bit code), `0xe1` = DEN + DTYPE + PMODE 11 (code DMA:
  3 host dwords → 2 instructions, exactly the host's `[A47..16, B15..0|A15..0, B47..16]` packing),
  `0x2040` = FLSH + PMODE 01, `0x41` = DEN + PMODE 01 (32-bit data DMA). MASTER=HSHAKE=EXTERN=0 → **slave-mode DMA**:
  every word written into EPB0 is DMA'd into internal memory at IIEP0, step IMEP0.
* SYSCON: HBW (bits 4-5) is 8-bit after reset in host-boot mode. 0 = 32-bit host bus, 0x900 = IMDW0 (block 0 40-bit
  data) + BHD (buffer-hang disable).
* Host boot: after reset the DSP idles with PC=0x8004, DMAC0=0x00A1 (DEN, instructions, 48-bit packing),
  IIEP0=0x8000, CEP0=0x100. When 256 instructions have arrived: EP0I → vector 0x8040, which must be **RTI** →
  execution resumes at **0x8005**.
* IRPTL/IMASK bits (vector = 0x8000 + 4·bit): 1 RSTI, 3 SOVFI, 4 TMZHI, 5 VIRPTI, 6 IRQ2I, **7 IRQ1I (0x801C)**,
  8 IRQ0I, 10-13 SPORT DMA, **16 EP0I (0x8040)**, **17 EP1I (0x8044)**, 21 CB7I, 22 CB15I, 23 TMZLI, 24-27 FP, 28-31 SFTx.
* MODE2: 0 IRQ0E, 1 IRQ1E, 2 IRQ2E (1 = edge), 5 TIMEN0, 7 PWMOUT0, 15-18 FLG0O-FLG3O.

---------------------------------------------------------------------------------------------------
## 2. (a) seg_rth: interrupt vector table and the boot loader at 0x8080 [V]

seg_rth (157 words) is byte-identical in puls2os0..5. It is the first section, sent as the 256-instruction boot
stream (padded with zeros). The vectors that matter:
```
08000  RTI
08001  NOP
08002  NOP
08003  NOP
08004  NOP
08005  JUMP 0x08080                                              ; -> wait_boot
08006  NOP
08007  NOP
08008  NOP
irq1_svc:
0801C  RTI
0801D  DM(0x0002A) = M5                                          ; IOP TCOUNT0
0801E  BIT SET MODE1 0x000004FC                                  ; {SRCU SRD1H SRD1L SRD2H SRD2L SRRFH SRRFL}
0801F  RTI
___lib_EP0I:
08040  RTI
08041  RTI
08042  RTI
08043  RTI
___lib_EP1I:
08044  RTI
08045  BIT CLR MODE1 0x00001000                                  ; {IRPTEN}
08046  PUSH STS
08047  RTI
```
* 0x8040 (EP0I) = RTI, as the manual requires: boot DMA done → RTI → 0x8005 → `JUMP 0x8080`.
* 0x801C (IRQ1) and 0x8044 (EP1I) hold "RTI; <delay-slot1>; <delay-slot2>; RTI". `os_initdma` later overwrites
  the first word with `JUMP x (DB)`, so the two following words become delay slots: IRQ1 → `TCOUNT0=0;
  MODE1|=SRCU..SRRFL` (secondary registers), and EP1I → `IRPTEN off; PUSH STS`.

Boot code:
```
wait_boot:
08080  R1 = 0x00000001
08081  DM(0x00002) = R1                                          ; IOP WAIT; <- 0x1
08082  R2 = 0x00000C00
08083  DM(0x0002E) = R2                                          ; IOP IOCTL; <- 0xC00 {DSDCTL DSDCK1}
08084  R0 = 0x00000000
08085  DM(0x00000) = R0                                          ; IOP SYSCON; <- 0x0 { HBW=0}
08086  DM(0x0DFFF) = R0
08087  R2 = 0x00002040
08088  DM(0x0001C) = R2                                          ; IOP DMAC0; <- 0x2040 {FLSH PMODE=01(32b)}
08089  R2 = 0x00020000
0808A  DM(0x00045) = R2                                          ; IOP EIEP0; <- 0x20000
0808B  DM(0x00046) = R0                                          ; IOP EMEP0; <- 0x0
0808C  R2 = 0x00000002
0808D  DM(0x00041) = R2                                          ; IOP IMEP0; <- 0x2
0808E  R7 = 0xFFFFFFFF
0808F  DM(0x00042) = R7                                          ; IOP CEP0; <- 0xFFFFFFFF
08090  R2 = 0x00000041
08091  DM(0x0001C) = R2                                          ; IOP DMAC0; <- 0x41 {DEN PMODE=01(32b)}
08092  L0 = 0x00000000
08093  I0 = 0x0000C000
08094  LCNTR = 0x1FFF, DO 0x08095 UNTIL LCE                      ; -> clr_loop
clr_loop:
08095  DM(I0,1) = R0
wait_loop:
08096  DM(0x00042) = R7                                          ; IOP CEP0
08097  R0 = DM(0x0DFFF)
08098  R0 = PASS R0
08099  IF EQ JUMP 0x08096                                        ; -> wait_loop
0809A  JUMP 0x08140 (DB)                                         ; -> os_start
0809B  R0 = 0x00000900
0809C  DM(0x00000) = R0                                          ; IOP SYSCON; <- 0x900 {IMDW0 BHD HBW=0}
```
What it does, in order:
1. WAIT=1; **IOCTL=0xC00** (disable SDRAM clocks/control: no SDRAM); **SYSCON=0** (32-bit host bus).
2. **DM[0xDFFF] = 0**: the release flag.
3. Re-arms EPB0 as an endless **slave** DMA for 32-bit data: DMAC0=0x2040 (flush), EIEP0=0x20000, EMEP0=0,
   **IMEP0=2**, **CEP0=0xFFFFFFFF**, DMAC0=0x41. It does **not** write IIEP0. The index for every host frame
   must therefore come from the card: the Cebulon writes IIEP0 from the frame's address field [L, because no
   other agent ever writes IIEP0 on the code/data paths, neither host nor DSP].
4. Clears DM 0xC000..0xDFFE (0x1FFF words).
5. `wait_loop`: rewrites CEP0=0xFFFFFFFF (so whatever count the host programs, the DMA never runs out), then
   polls **DM 0xDFFF** until it is non-zero. Meanwhile the host's IOP writes (0x1c/0x42/0x41/0x1c) and data frames
   load seg_init/seg_pmco/seg_dmda through EPB0.
6. Once DM 0xDFFF != 0: `JUMP os_start (DB)` with **SYSCON=0x900** (IMDW0, BHD) in the delay slots.

Does it match the host IOP sequence? **Yes [V].** The code path is `0x1c=0x20e0, 0x42=n, 0x41=1, 0x1c=0xe1`
(48-bit code, stride 1). The data path is `0x1c=0x2040, 0x42=n, 0x1c=0x41` (32-bit data; IMEP0 is set to 1/2 by the
host per chunk). The Run step on DSP0 (`0x1c=0x2040, 0x41=2, 0x42=-1, 0x1c=0x41`) is the same programming that
the boot code and `os_initdma` do themselves. The boot loader depends on **nothing** the host patches. It
depends only on the 0xDFFF word, and the host must write that last (Run), after all sections.

`loadPX1` (0x80D6, in seg_init, not in the boot code) is `R6 = 0x00xxC3F0` with byte[3] = 0x08 or 0x0A:
```
080D6  R6 = 0x0008C3F0     ;; byte3 patched: 0x08 (DSP0-4) / 0x0A (last DSP) -> 0x000AC3F0
080D7  R2 = 0x00040000
080D8  R2 = R2 OR R6       ;; 0x000CC3F0 / 0x000EC3F0
080D9  DM(0x0C50B) = R2    ;; DSP0: finish_eom+1  (DSP1-5: DM(0xC50A)=R2, DM(0xC508)=R6)
080DA  DM(0x0C509) = R2    ;; sync_eom
```
It builds the **end-of-message (EOM) header word** that each DSP appends to its sync-output DMA every word clock.
In the DSP→bus header encoding (`len<<25 | dsp<<21 | (dsp&0x10)<<14 | addr`), 0xCC3F0 is addr 0xC3F0, bit 18
(DSP id 16 = the Cebulon [L]) and bit 19. The last DSP (0x0A) also sets **bit 17 (0x20000)**, the same bit the host
uses in its "batch/flush" headers. Interpretation [L]: 0x08 = "pass the sync token to the next DSP", 0x0A = "end of
round, back to the Cebulon/host". It does not affect boot. The loader already patches it like Sim2k.
`call_serCommSetDMA` does not exist in any puls2os image, so that patch is a no-op on Pulsar2 [V].

---------------------------------------------------------------------------------------------------
## 3. (b) OS init: `os_start` → `os_init` → `os_initreg` / `os_initdma` [V]

```
os_initreg:
080BA  BIT SET MODE1 0x000004FC                                  ; {SRCU SRD1H SRD1L SRD2H SRD2L SRRFH SRRFL}
080BB  CALL 0x0809D                                              ; -> initregs
080BC  BIT CLR MODE1 0x000004FC                                  ; {SRCU SRD1H SRD1L SRD2H SRD2L SRRFH SRRFL}
080BD  CALL 0x0809D                                              ; -> initregs
080BE  MODE1 = 0x00000000                                        ; {}
080BF  USTAT2 = DM(0x0002F)                                      ; IOP IOSTAT
080C0  RTS
jmp_os_firstsync:
080C1  JUMP 0x081A3 (DB)                                         ; -> os_firstsync
jmp_os_epb1_int:
080C2  JUMP 0x081B5 (DB)                                         ; -> os_epb1_int
os_initdma:
080C3  PX = PM(0x080C1)                                          ; jmp_os_firstsync
080C4  PM(0x0801C) = PX                                          ; irq1_svc
080C5  PX = PM(0x080C2)                                          ; jmp_os_epb1_int
080C6  PM(0x08044) = PX                                          ; ___lib_EP1I
080C7  R0 = 0x00020000
080C8  DM(0x00045) = R0                                          ; IOP EIEP0; <- 0x20000
080C9  DM(0x0004D) = R0                                          ; IOP EIEP1; <- 0x20000
080CA  DM(0x00046) = M5                                          ; IOP EMEP0
080CB  R0 = 0x00000002
080CC  DM(0x00041) = R0                                          ; IOP IMEP0; <- 0x2
080CD  R0 = 0x00000001
080CE  DM(0x00049) = R0                                          ; IOP IMEP1; <- 0x1
080CF  DM(0x0004E) = M5                                          ; IOP EMEP1
080D0  DM(0x0000D) = R0                                          ; IOP MSGR5; <- 0x1
080D1  R0 = 0xFFFFFFFF
080D2  DM(0x00042) = R0                                          ; IOP CEP0; <- 0xFFFFFFFF
080D3  DM(0x00047) = R0                                          ; IOP ECEP0; <- 0xFFFFFFFF
080D4  R0 = 0x00000041
080D5  DM(0x0001C) = R0                                          ; IOP DMAC0; <- 0x41 {DEN PMODE=01(32b)}
loadPX1:
080D6  R6 = 0x0008C3F0
080D7  R2 = 0x00040000
080D8  R2 = R2 OR R6
080D9  DM(0x0C50B) = R2                                          ; finish_eom+1
080DA  DM(0x0C509) = R2                                          ; sync_eom
080DB  R0 = DM(0x0C409)                                          ; dspID
080DC  R2 = 0x00000020
080DD  R1 = R0 * R2 (SSI)
080DE  R2 = 0x0000C080
080DF  CALL 0x08246 (DB)                                         ; -> updateTCBCounter
080E0  R0 = R2 + R1
080E1  R5 = 0x00000003
080E2  R1 = 0x0000C50A
080E3  R0 = 0x00000002
080E4  DM(0x0004A) = R0                                          ; IOP CEP1; <- 0x2
080E5  DM(0x0004F) = R0                                          ; IOP ECEP1; <- 0x2
080E6  DM(0x00048) = R1                                          ; IOP IIEP1; <- 0xC50A
080E7  R0 = 0x00000205
080E8  DM(0x0001D) = R0                                          ; IOP DMAC1; <- 0x205 {DEN TRAN MASTER PMODE=00}
080E9  DM(0x0000D) = M5                                          ; IOP MSGR5
080EA  R0 = DM(0x0C409)                                          ; dspID
080EB  R0 = R0 + R0
080EC  R1 = 0x63E00800
080ED  R1 = R0 + R1
080EE  DM(0x0C40A) = R1                                          ; dspAckDest
080EF  BIT SET MODE2 0x00000003                                  ; {IRQ0E IRQ1E}
080F0  BIT CLR MODE2 0x00000004                                  ; {IRQ2E}
080F1  BIT CLR IRPTL 0x00020080                                  ; {IRQ1I EP1I}
080F2  BIT SET IMASK 0x00020080                                  ; {IRQ1I EP1I}
080F3  R0 = 0xFFFFFFFF
080F4  BIT CLR IMASK 0x00010000                                  ; {EP0I}
080F5  DM(0x00028) = R0                                          ; IOP TPERIOD0; <- 0xFFFFFFFF
080F6  DM(0x00029) = R0                                          ; IOP TPWIDTH0; <- 0xFFFFFFFF
080F7  DM(0x0002A) = R0                                          ; IOP TCOUNT0; <- 0xFFFFFFFF
080F8  BIT SET MODE2 0x000000A0                                  ; {TIMEN0 PWMOUT0}
080F9  BIT SET MODE1 0x00001000                                  ; {IRPTEN}
080FA  RTS
os_init:
080FB  BIT CLR MODE2 0x00000020                                  ; {TIMEN0}
080FC  CALL 0x080BA                                              ; -> os_initreg
080FD  I0 = 0x0000C080
080FE  R0 = 0x00000000
080FF  LCNTR = 0x0380, DO 0x08100 UNTIL LCE                      ; -> clr_loop
clr_loop:
08100  DM(I0,M6) = R0
08101  CALL 0x080C3                                              ; -> os_initdma
08102  USTAT1 = DM(0x0002E)                                      ; IOP IOCTL
08103  BIT SET USTAT1 0x00000080
08104  DM(0x0002E) = USTAT1                                      ; IOP IOCTL
08105  BIT CLR USTAT2 0x00000080
08106  DM(0x0002F) = USTAT2                                      ; IOP IOSTAT
08107  LCNTR = 0x000A, DO 0x08108 UNTIL LCE                      ; -> bp_wait
bp_wait:
08108  NOP
08109  R1 = DM(0x0002F)                                          ; IOP IOSTAT
0810A  BIT SET USTAT2 0x00000080
0810B  DM(0x0002F) = USTAT2                                      ; IOP IOSTAT
0810C  R0 = FEXT R1 BY 0:5
0810D  BTST R1 BY 6
0810E  IF NOT SZ R0 = BSET R0 BY 5
0810F  DM(0x0C508) = R0                                          ; backplateID
08110  RTS
```
In order:
1. `MODE2 &= ~TIMEN0`; `os_initreg`: initialise M/L/I registers in both register sets (MODE1 SRxx on/off),
   MODE1=0, USTAT2=IOSTAT.
2. Clear DM 0xC080..0xC3FF (0x380 words, the inter-DSP/PC "comm" slot area, = pluto vt+0xf8/+0xfc).
3. `os_initdma`:
   * patch the IRQ1 vector → `JUMP os_firstsync (DB)` and the EP1I vector → `JUMP os_epb1_int (DB)`;
   * EPB0 (ch 8): EIEP0=0x20000, EMEP0=0, **IMEP0=2**, CEP0=ECEP0=-1, DMAC0=0x41 (endless 32-bit slave RX);
   * EPB1 (ch 9): EIEP1=0x20000, EMEP1=0, IMEP1=1;
   * **MSGR5 = 1**;
   * `loadPX1` → sync_eom/finish_eom; `updateTCBCounter(R0=0xC080+dspID*0x20, R5=3)` builds the sync-output
     descriptor;
   * **DSP0 only**: CEP1=ECEP1=2, IIEP1=0xC50A, **DMAC1=0x205** (DEN|TRAN|MASTER = master-mode DMA, internal →
     external 0x20000), i.e. DSP0 immediately transmits `[0, EOM]`, which starts the token ring. Then
     **MSGR5 = 0** (DSP0 waits for the token). DSP1..5 write CEP1=ECEP1=DMAC1=0 and **keep MSGR5 = 1**.
   * **dspAckDest = 0x63E00800 + 2·dspID**: computed by the OS from the patched `dspID`. The host's later
     SetValue writes the same value.
   * MODE2 |= IRQ0E|IRQ1E (edge), IRQ2E cleared; IRPTL clear IRQ1I|EP1I; **IMASK |= IRQ1I | EP1I**, IMASK &= ~EP0I.
     No timer interrupt and no SPORT interrupt on DSP0-3 (DSP4: sync-plate timer; DSP5: IRQ0 for the µC).
   * TPERIOD0=TPWIDTH0=TCOUNT0=0xFFFFFFFF, MODE2 |= TIMEN0|PWMOUT0 (timer 0 = free-running cycle counter, no IRQ).
   * **IRPTEN = 1**, RTS.
4. DSP0 only: IOCTL |= FLG11O; pulse FLAG11 (IOSTAT bit 7); after 10 NOPs read IOSTAT: **backplateID** =
   FLAG4..8 | (FLAG10 ? 0x20 : 0). DSP4: sync-plate detection (`init_syncPlate`, FLAG0/1 via MODE2/ASTAT).
   DSP5: `ucInit` (IRQ0 vector → `ucR`, IOCTL all flags outputs). DSP2: sets FLAG2 output.
5. Return to `os_start` → main loop.

**Nothing in init waits for anything** (no IRQ, no flag, no memory poll). The only wait in the whole boot path is
the 0xDFFF poll in `wait_loop`. SDRAM: none (IOCTL=0xC00; SDRDIV never written). SYSCON: 0 → 0x900.

---------------------------------------------------------------------------------------------------
## 4. (c) How sysmsgs are processed, and how the ack gets back [V]

### 4.1 Main loop: sysmsgs are **polled**, not interrupt driven
```
os_start:
08140  CALL 0x080FB                                              ; -> os_init
sloop:
08141  CALL 0x081C8                                              ; -> eo_epb1_int/os_async
08142  CALL 0x08154                                              ; -> os_sys_async
08143  JUMP 0x08146 (DB)                                         ; -> no_interrupt_async
08144  R1 = DM(0x0002A)                                          ; IOP TCOUNT0
08145  R0 = DM(0x0C400)                                          ; sysvars/wclk
no_interrupt_async:
08146  DM(0x0C411) = R1                                          ; asTCount
08147  R1 = DM(0x0C40F)                                          ; alastClk
08148  R2 = R0 - R1
08149  DM(0x0C40E) = R2                                          ; asyncCnt
0814A  R2 = DM(0x0C410)                                          ; asRatio
0814B  R1 = R1 + R2
0814C  DM(0x0C40F) = R1                                          ; alastClk
waitloop:
0814D  R0 = R0 - R1
0814E  R0 = PASS R0
0814F  IF GE JUMP 0x08141                                        ; -> sloop
08150  CALL 0x08154                                              ; -> os_sys_async
08151  JUMP 0x0814D (DB)                                         ; -> waitloop
08152  R1 = DM(0x0C40F)                                          ; alastClk
08153  R0 = DM(0x0C400)                                          ; sysvars/wclk
os_sys_async:
08154  CALL 0x081D2                                              ; -> os_sysmsg
08155  RTS
```
`sloop` runs the async module chain (`_firstasync`), then calls `os_sysmsg`. In `waitloop` it keeps calling
`os_sysmsg` until `wclk` (incremented by the IRQ1 sync handler) passes `alastClk + asRatio`. So **sysmsgs are
handled whenever the core is in the main loop**, independent of the word clock.

### 4.2 `os_sysmsg` and handlers
```
os_sysmsg:
081D2  I0 = 0x0000C419                                           ; =&sysMsg
081D3  R1 = DM(0x3,I0)
081D4  R1 = PASS R1
081D5  IF LE RTS
081D6  I1 = 0x0000C516                                           ; =&sysmsgJumpTbl
081D7  R0 = 0x00000014
081D8  COMP(R1, R0), M1 = R1
081D9  IF GE RTS
081DA  I8 = DM(M1,I1)
081DB  JUMP (M13,I8)
sys_ret:
081DC  DM(0x0C41C) = M5                                          ; sysMsg+3
081DD  JUMP 0x08279 (DB)                                         ; -> sendmsgPX2toR7
081DE  PX2 = 0x00000001
081DF  R7 = DM(0x0C40A)                                          ; dspAckDest
sys_readvalue:
081E0  R1 = DM(0x1,I0)
081E1  R0 = DM(0x2,I0)
081E2  R0 = PASS R0, I1 = R1
081E3  IF EQ JUMP 0x081EC                                        ; -> readAsync
081E4  JUMP 0x081E7 (DB)                                         ; -> readvalue_mode1_latency
081E5  BIT CLR MODE1 0x00001000                                  ; {IRPTEN}
081E6  R0 = DM(0x0C400)                                          ; sysvars/wclk
readvalue_mode1_latency:
081E7  R0 = FEXT R0 BY 0:1
081E8  M0 = R0
081E9  JUMP 0x081ED (DB)                                         ; -> valueRead
081EA  BIT SET MODE1 0x00001000                                  ; {IRPTEN}
081EB  R0 = DM(M0,I1)
readAsync:
081EC  R0 = DM(M5,I1)
valueRead:
081ED  PX2 = R0
081EE  CALL 0x08279 (DB)                                         ; -> sendmsgPX2toR7
081EF  R7 = DM(0x0C40A)                                          ; dspAckDest
081F0  R7 = R7 + 1
081F1  JUMP 0x081DC                                              ; -> sys_ret
sys_setvalue:
081F2  I1 = DM(0x1,I0)
081F3  JUMP 0x081DC (DB)                                         ; -> sys_ret
081F4  R1 = DM(0x2,I0)
081F5  DM(0x0,I1) = R1
sys_clearMem:
08263  I1 = DM(0x1,I0)
08264  R1 = DM(0x2,I0)
08265  LCNTR = R1, DO 0x08266 UNTIL LCE                          ; -> clearMemLoop
clearMemLoop:
08266  DM(I1,M6) = M13
08267  JUMP 0x081DC                                              ; -> sys_ret
```
* Block at **sysMsg = DM 0xC419**: **+1 = a, +2 = b, +3 = type**. `type` must be 1..0x13 (`IF LE RTS`, `IF GE RTS`),
  dispatched through `sysmsgJumpTbl` (DM 0xC516). Index 1 sys_setvalue, 2 sys_addmodule, 3 sys_movemodule,
  4/5 add/delsyncmsg (nop), 6 sys_patchinstruction, 7 sys_callfunction, **8 sys_readvalue (GetValue)**,
  9 sys_loadcode, 0xA/0xB sys_syncmsghead/tail (`updateTCBCounter`), 0xC nop, 0xD/0xE/0xF
  sendwclk/setcommwords/loadSAT (nop on Pulsar2), 0x10 enableXTC (nop), **0x11 sys_clearMem**, 0x12 sys_setFlags
  (clears the type, **no ack**), 0x13 bootXDSP (nop). Every handler except setFlags ends in `sys_ret` (ack). These agree with Sim2k's use: 2 = connect, 6 = patch, 8 = GetValue, 0x11 = clearMem.
* GetValue (type 8, a=addr, b=0): reads DM(a), sends `PX2=value` to `dspAckDest+1` (host word 0x801+2n), then
  `sys_ret` **clears DM 0xC41C** and sends `PX2=1` to `dspAckDest` (host word 0x800+2n). The host polls 0x800+2n.

### 4.3 Sending: messages are only **queued**; they leave in the IRQ1 (word clock) handler
```
sendmsgPX2toR7:
08279  R0 = FEXT R7 BY 21:4
0827A  R1 = DM(0x0C409)                                          ; dspID
0827B  COMP(R0, R1)
0827C  IF EQ JUMP 0x082A5                                        ; -> sendToMe
sendmsgWait:
0827D  NOP
0827E  NOP
0827F  NOP
08280  JUMP 0x08283 (DB)                                         ; -> no_interrupt2
08281  BIT CLR MODE1 0x00001000                                  ; {IRPTEN}
08282  R1 = DM(0x0C402)                                          ; msgfifo
no_interrupt2:
08283  R0 = DM(0x0C403)                                          ; fifoTop
08284  R0 = R0 - R1
08285  R1 = DM(0x0C507)                                          ; fifoFulSize
08286  COMP(R0, R1)
08287  IF GE JUMP 0x0827D (DB)                                   ; -> sendmsgWait
08288  BIT SET MODE1 0x00001000                                  ; {IRPTEN}
08289  R0 = DM(0x0C412)                                          ; asMsgs
0828A  R0 = R0 + 1
0828B  DM(0x0C412) = R0                                          ; asMsgs
0828C  BTST R7 BY 30
0828D  IF NOT SZ JUMP 0x0829E                                    ; -> msgToPC
0828E  JUMP 0x08291 (DB)                                         ; -> no_interrupt3
0828F  BIT CLR MODE1 0x00001000                                  ; {IRPTEN}
08290  I7 = DM(0x0C403)                                          ; fifoTop
no_interrupt3:
08291  DM(I7,M6) = R7
08292  DM(I7,M6) = PX2
08293  DM(I7,M6) = 0x0FE0C030
08294  DM(I7,M6) = 0x77777777
08295  DM(I7,M6) = 0x66666666
08296  DM(I7,M6) = 0x55555555
08297  DM(I7,M6) = 0x44444444
08298  DM(I7,M6) = 0x33333333
08299  DM(I7,M6) = 0x22222222
0829A  DM(I7,M6) = 0x11111111
msgSentToR7:
0829B  RTS (DB,LR)
0829C  BIT SET MODE1 0x00001000                                  ; {IRPTEN}
0829D  DM(0x0C403) = I7                                          ; fifoTop
msgToPC:
0829E  R0 = BCLR R7 BY 29
0829F  JUMP 0x082A2 (DB)                                         ; -> no_interrupt4
082A0  BIT CLR MODE1 0x00001000                                  ; {IRPTEN}
082A1  I7 = DM(0x0C403)                                          ; fifoTop
no_interrupt4:
082A2  JUMP 0x0829B (DB)                                         ; -> msgSentToR7
082A3  DM(I7,M6) = R0
082A4  DM(I7,M6) = PX2
sendToMe:
082A5  R0 = FEXT R7 BY 0:16
082A6  RTS (DB,LR)
082A7  I7 = R0
082A8  DM(M5,I7) = PX2
```
`sendmsgPX2toR7`: if the destination DSP field (bits 21-24) equals our own dspID, it writes locally. Otherwise
it spins (interrupts off/on) until `msgfifo` (DM 0xC47E..) has room, then appends the frame. For host targets
(bit 30 set, e.g. 0x63E00800) that is 2 words `[R7 & ~bit29, PX2]`; otherwise 10 words with the
0x0FE0C030/0x7777…/0x1111 padding. **Nothing is transmitted here.**

### 4.4 IRQ1 = sync interrupt (word clock [L]); transmission over EPB1 master DMA
```
os_sync:
08156  BIT CLR MODE1 0x00008003                                  ; {BR8 BR0 TRUNC}
firstClockOk:
08157  I0 = 0x0000C400                                           ; =&sysvars/wclk
wait_msg:
08158  R0 = DM(0x0000D)                                          ; IOP MSGR5
08159  R0 = PASS R0
0815A  IF EQ JUMP 0x08158                                        ; -> wait_msg
msgr5_ok:
0815B  DM(0x0000D) = M5                                          ; IOP MSGR5
0815C  R1 = 0x00002204
0815D  DM(0x0001D) = R1                                          ; IOP DMAC1; <- 0x2204 {TRAN MASTER FLSH PMODE=00}
0815E  R9 = DM(0x9,I0)
0815F  R10 = DM(0xB,I0)
08160  COMP(R9, R10), R5 = M5
08161  IF NE JUMP 0x0816E [PC+13], ELSE R5 = R5 + 1              ; -> noAsyncCom
08162  DM(0x0C512) = M6                                          ; async_sent
08163  I1 = DM(0x0C403)                                          ; fifoTop
08164  R2 = DM(0x0C402)                                          ; msgfifo
08165  R6 = DM(0x0C509)                                          ; sync_eom
08166  DM(I1,M6) = R6
08167  R1 = I1
08168  R1 = R1 - R2, I1 = R2
08169  DM(0x0C510) = R2                                          ; async_base
0816A  DM(0x0C511) = R1                                          ; async_size
0816B  R0 = DM(0xFFFFFFFF,I1)
0816C  R0 = R0 + 1, DM(2,I0) = R0
0816D  DM(0x3,I0) = R0
noAsyncCom:
0816E  R3 = 0x00000002
0816F  I6 = DM(0x0C50F)                                          ; lastWClkI7
08170  R0 = DM(0x0C50E)                                          ; sync_size
08171  R1 = DM(0xC50C,I6)
08172  DM(0x00048) = R1                                          ; IOP IIEP1
08173  R0 = R0 - R5
08174  DM(0x0004A) = R0                                          ; IOP CEP1
08175  DM(0x0004F) = R0                                          ; IOP ECEP1
08176  DM(0x00049) = R3                                          ; IOP IMEP1; <- 0x2
08177  BIT CLR IRPTL 0x00020000                                  ; {EP1I}
08178  R0 = 0x00000205
08179  DM(0x0001D) = R0                                          ; IOP DMAC1; <- 0x205 {DEN TRAN MASTER PMODE=00}
0817A  DM(0x00042) = M7                                          ; IOP CEP0
0817B  DM(0x0C413) = IMASK                                       ; SaveIMask
0817C  IMASK = DM(0x0C407)                                       ; imaskIRQ
0817D  JUMP 0x08180 (DB,CI)                                      ; -> allowInts
0817E  FLUSH CACHE
0817F  NOP
allowInts:
08180  PUSH STS
08181  BIT SET MODE1 0x000004FC                                  ; {SRCU SRD1H SRD1L SRD2H SRD2L SRRFH SRRFL}
08182  BIT CLR MODE1 0x00000003                                  ; {BR8 BR0}
08183  R6 = DM(0x0C400)                                          ; sysvars/wclk
08184  R6 = R6 + 1, R0 = M6
08185  R7 = R6 AND R0, DM(0,I0) = R6
08186  I7 = R7
08187  DM(0x0C50F) = R7                                          ; lastWClkI7
08188  R0 = DM(0xB,I0)
08189  R0 = R0 + 1, R1 = DM(12,I0)
0818A  COMP(R0, R1)
0818B  IF GE R0 = R0 - R0
0818C  DM(0xB,I0) = R0
hookReturnJump:
0818D  JUMP 0x0818E                                              ; -> hookReturn
hookReturn:
0818E  I8 = DM(0x0C406)                                          ; _firstsync
0818F  JUMP (M13,I8) (DB)
08190  DM(0x0C417) = PX1                                         ; SyncSavPX1
08191  DM(0x0C418) = PX2                                         ; SyncSavPX2
ret_sync:
08192  PX1 = DM(0x0C417)                                         ; SyncSavPX1
08193  PX2 = DM(0x0C418)                                         ; SyncSavPX2
08194  R0 = DM(0x0002A)                                          ; IOP TCOUNT0
08195  R0 = -R0
08196  R1 = DM(0x0C40D)                                          ; _tcount
08197  R0 = MIN(R0, R1)
08198  DM(0x0C40D) = R0                                          ; _tcount
08199  BIT TST IRPTL 0x00000080                                  ; {IRQ1I}
0819A  IF TF JUMP 0x0819E                                        ; -> eo_sync/rec_interrupt
cont_irq:
0819B  RTS (DB,LR)
0819C  POP STS
0819D  IMASK = DM(0x0C413)                                       ; SaveIMask
eo_sync/rec_interrupt:
0819E  BIT SET MODE2 0x00020000                                  ; {FLG2O}
0819F  BIT TGL ASTAT 0x00200000
081A0  JUMP 0x0819B (DB)                                         ; -> cont_irq
081A1  BIT TGL ASTAT 0x00200000
081A2  BIT CLR MODE2 0x00020000                                  ; {FLG2O}
os_firstsync:
081A3  BIT CLR MODE1 0x00008003                                  ; {BR8 BR0 TRUNC}
081A4  DM(0x0C417) = PX1                                         ; SyncSavPX1
081A5  DM(0x0C418) = PX2                                         ; SyncSavPX2
081A6  PX = PM(0x081B0)                                          ; jmp_os_sync
081A7  PM(0x0801C) = PX                                          ; irq1_svc
081A8  PX1 = DM(0x0C417)                                         ; SyncSavPX1
081A9  PX2 = DM(0x0C418)                                         ; SyncSavPX2
081AA  R0 = DM(0x0000D)                                          ; IOP MSGR5
081AB  R0 = PASS R0
081AC  IF NE JUMP 0x08157                                        ; -> firstClockOk
081AD  R0 = 0x00000002
081AE  DM(0x0C400) = R0                                          ; sysvars/wclk
081AF  RTI
jmp_os_sync:
081B0  JUMP 0x08156 (DB)                                         ; -> os_sync
```
```
SaveR0EPB1:
081B4  NOP
os_epb1_int:
081B5  PM(0x081B4) = R0                                          ; SaveR0EPB1
081B6  R0 = DM(0x0C512)                                          ; async_sent
081B7  R0 = R0 - 1
081B8  IF LT JUMP 0x081C4                                        ; -> no_more_dma
081B9  DM(0x0C512) = R0                                          ; async_sent
081BA  R0 = 0x00000204
081BB  DM(0x0001D) = R0                                          ; IOP DMAC1; <- 0x204 {TRAN MASTER PMODE=00}
081BC  R0 = DM(0x0C510)                                          ; async_base
081BD  DM(0x00048) = R0                                          ; IOP IIEP1
081BE  R0 = DM(0x0C511)                                          ; async_size
do_asyncdma:
081BF  DM(0x00049) = M6                                          ; IOP IMEP1
081C0  DM(0x0004A) = R0                                          ; IOP CEP1
081C1  DM(0x0004F) = R0                                          ; IOP ECEP1
081C2  R0 = 0x00000205
081C3  DM(0x0001D) = R0                                          ; IOP DMAC1; <- 0x205 {DEN TRAN MASTER PMODE=00}
no_more_dma:
081C4  POP STS
081C5  RTI (DB)
081C6  BIT SET MODE1 0x00001000                                  ; {IRPTEN}
081C7  R0 = PM(0x081B4)                                          ; SaveR0EPB1
```
* The first IRQ1 runs `os_firstsync`: it swaps the IRQ1 vector to `os_sync`. If **MSGR5 == 0** it sets wclk=2 and
  returns, otherwise it falls into the normal sync code.
* Every later IRQ1 runs `os_sync`: **`wait_msg` spins inside the ISR until MSGR5 != 0** (token from the
  Cebulon/previous DSP [L]). It then clears MSGR5 and stops EPB1. If `curDSP == dspID` (round robin; `curDSP`
  counts 0..numDSP-1, `numDSP`=6 is preset in the image), it hands `msgfifo` to the async DMA. It then programs
  EPB1 with the sync outputs (IIEP1 = sync_base[lastWClkI7], IMEP1=2, CEP1=sync_size-R5, DMAC1=0x205 master
  TX → external 0x20000), re-arms CEP0=-1, increments `wclk`, and runs the sync module chain (`_firstsync` →
  `ret_sync`).
* EP1I (`os_epb1_int`): when the sync DMA is done and `async_sent` is set, it DMAs `async_base/async_size` (the
  queued messages, including the **ack**) out through EPB1.

**Host-side conditions for an ack to reach host SRAM 0x800+2n:**
1. The DSP has left `wait_loop` (DM 0xDFFF written, see §0 bug 1).
2. The sysmsg is in DM 0xC41A..0xC41C with the type last, at 0xC41C (§0 bug 2).
3. The word clock is running and raising IRQ1 on every DSP (Run: reg0 |= 0x10 etc.).
4. The card's sync engine hands MSGR5 tokens round the DSP ring. DSP0's initial `[0, EOM]` DMA starts it. Each
   DSP only ships its message FIFO in its own round-robin slot, so an ack can take up to numDSP=6 word-clock
   periods (plus queueing). This is far below the 1 s host timeout.
5. The Cebulon routes EPB1 master writes (header 0x63E00800|2n → DSP id 0xF = host) into BAR SRAM 0x80000+4·(0x800+2n).
   This is hardware; Windows uses the same frames, so it is expected to work once 1 and 2 are fixed [L].

---------------------------------------------------------------------------------------------------
## 5. (d) Hypotheses, ranked, with the concrete test

1. **[V] No DSP is ever released from `wait_loop`.** The loader writes the release/stack word to 0x27FFF instead
   of **0xDFFF** (wrong DSP class: `sharc` instead of `pluto`). Evidence: `wait_boot` @0x8086/0x8097-0x8099;
   `FUN_10c32880` → `FUN_10c3c100` (vtable 0x10c8a634, slots +0xe0/+0xe4 = 0xC400 / 0xE000-0xC400); `FUN_10c3aa50`
   writes 1 at dmstart+dmsize-1; Linux FIFO dump `22027fff 1`. This explains every symptom: frames are consumed
   (EPB0 slave DMA with CEP0 forced to -1 accepts everything), the word clock runs (card hardware), and nothing ever
   answers.
   **Test:** `DM_START, DM_SIZE = 0xC400, 0x1C00` in pulsar_loader.py, so Run writes `[1]` to 0xDFFF.
2. **[V] The sysmsg goes to the wrong address and in the wrong word order.** It must be `{a, b, type}` at `sysMsg+1`
   = 0xC41A (FUN_10c33410 asm; OS @0x81D2-0x81DB reads type at +3). Without this fix GetValue still times out
   even after fix 1.
   **Test:** `upload_data(dsp, [a, b, mtype], syms["sysMsg"] + 1)`.
   Optional check after fix 1: SetValue/GetValue of `dspID` (0xC409).
3. **[L] The sync token ring (MSGR5) does not circulate.** If IRQ1 fires but no MSGR5 token arrives, every DSP
   hangs inside the `os_sync` ISR at 0x8158 from the 2nd word-clock IRQ onwards: the main loop dies and no ack
   is ever sent. This depends on card logic set up by host registers (reg0 0x40/0x80, reg1 mask, SRAM master
   words `0x8400C300/0x8400C301` → DSP DM 0xC300, the PC sync window inside the 0xC080..0xC3FF area the OS
   clears). Bus master (reg0 0x80) is off by default in our loader, but Windows sets it before starting the clock.
   **Test (after 1 and 2):** if acks still time out, run `diag` and watch reg0 bits 0-5 (the "stalled at DSP n"
   pointer). A pointer stuck at one DSP means the ring is broken. Then try `--bus-master` (Windows order:
   `bar[0x20002..0x203ff]=0`, reg0|=0x80 *before* reg1/reg0|=0x40 and startClk).
4. **[?] DSP1..5 not booted.** The boot stream goes only to DSP0 (raw header bit 31). If bit 31 does not mean
   broadcast on this card, DSP1-5 never execute `wait_boot`. Windows sends the identical stream, so this is unlikely.
   It would show up as only DSP0 answering after fixes 1 and 2.
5. **[?] Timing.** The release word for DSP n is written while DSP n+1.. are still being loaded. That is harmless,
   because each DSP only polls its own 0xDFFF. Sim2k sleeps 10 ms before the DSP0 IOP re-arm. Keep the existing
   sleeps.

Note: the user reported that our frames match "a real Windows FIFO capture" word for word. That cannot include
the Run/stack-word and sysmsg frames: Windows writes those to 0xDFFF and 0xC41A
(`(dsp|0x10)<<21|0xDFFF` and `((3<<4|dsp)<<21)|0xC41A` inside the wrapped/batched form). Worth re-checking the
capture for exactly these two frames.

Other pluto-specific host behaviour to replicate later (not boot-critical) [V]:
* dsp vt+0x10 (`FUN_10c3c230`, DSP start) and vt+0x6c (`FUN_10c3c290`, set rate) send **sysmsg 0xB**
  (`sys_syncmsghead`: a = numCommSlots+3, b = 0xC080 + 0x20·dsp, c = -1), and SetValue 0 on the comm-slot words
  `0xC080+0x20n+4+2i` / `+5+2i`.
* `allocSyncOutput` slots are 0xC000-based (the pluto ctor initialises the slot table entries to 0xC000).
* UploadData's `sys_clearMem` shortcut (sysmsg 0x11 for zero runs > 0x20 words) also uses this sysmsg path, so it
  must only be used once the OS runs (state 2).

---------------------------------------------------------------------------------------------------
## 6. Reference: OS variables (DM, puls2os0) and updateTCBCounter

```
0C400  0000000100  sysvars/wclk
0C401  4000000000  FScale
0C402  0000C47E00  msgfifo
0C403  0000C47F00  fifoTop
0C404  0000000000  _firstmod
0C405  0000000000  _firstasync
0C406  0000819200  _firstsync
0C407  0002000000  imaskIRQ
0C408  8000000000  cmdMask
0C409  0000000000  dspID
0C40A  0000000000  dspAckDest
0C40B  0000000000  curDSP
0C40C  0000000600  numDSP
0C40D  0000000000  _tcount
0C40E  0000000000  asyncCnt
0C40F  0000000000  alastClk
0C410  0000001000  asRatio
0C411  0000000000  asTCount
0C412  0000000000  asMsgs
0C413  0000000000  SaveIMask
0C414  0000000000  SavI0SendMsg
0C415  0000000000  SavR0SendMsg
0C416  0000000000  SavR1SendMsg
0C417  0000000000  SyncSavPX1
0C418  0000000000  SyncSavPX2
0C419  0000000000  sysMsg
0C41A  0000000000
0C41B  0000000000
0C41C  0000000000
0C507  0000003900  fifoFulSize
0C508  FFFFFFFF00  backplateID
0C509  0002C70000  sync_eom
0C50A  0000000000  finish_eom
0C50B  000CC70000
0C50C  0000C08000  sync_base
0C50D  0000C08100
0C50E  0000000000  sync_size
0C50F  0000000000  lastWClkI7
0C510  0000C47E00  async_base
0C511  0000000100  async_size
0C512  0000000000  async_sent
0C513  0000000000  os_px1Shift
0C514  0000000000  input9/input8/input7/input6/input5/input4/input3/input2/input1/input0/_null
0C515  0000000000
0C516  000081DC00  sysmsgJumpTbl
0C517  000081F200
0C518  0000821800
0C519  0000822400
0C51A  0000824100
0C51B  0000824100
0C51C  000081F600
0C51D  0000821300
0C51E  000081E000
0C51F  0000822E00
0C520  0000824200
0C521  0000824200
0C522  000081DC00
0C523  000082DF00
0C524  000082DF00
0C525  000082DF00
0C526  0000826200
0C527  0000826300
0C528  0000826800
0C529  0000826B00
```
```
updateTCBCounter:
08246  I1 = 0x0000C50C                                           ; =&sync_base
08247  R1 = R5 - 1, I2 = R0
08248  R2 = DM(0x0C408)                                          ; cmdMask
08249  R0 = R0 OR R2, I3 = I2
0824A  R1 = R1 - 1, R12 = I2
0824B  R1 = R1 - 1, R3 = R1
0824C  IF LE R0 = R0 - R0
0824D  R0 = R0 OR FDEP R1 BY 25:5
0824E  R1 = FEXT R1 BY 5:27
0824F  R0 = R0 OR FDEP R1 BY 22:3
08250  R3 = R3 + R3, R11 = DM(I3,2)
08251  M2 = R3
08252  R11 = DM(0x0C509)                                         ; sync_eom
08253  R3 = 0x00000005
08254  MODIFY(I3,M2)
08255  JUMP 0x08258 (DB)                                         ; -> no_interrupt1
08256  BIT CLR MODE1 0x00001000                                  ; {IRPTEN}
08257  R12 = R12 + 1, DM(0,I1) = R12
no_interrupt1:
08258  DM(0x1,I1) = R12
08259  DM(M5,I3) = R11
0825A  DM(M6,I3) = R11
0825B  R11 = R11 - R11, DM(2,I1) = R5
0825C  DM(I2,1) = R11
0825D  R0 = R0 + R3, DM(I2,1) = R11
0825E  R0 = R0 - 1, DM(1,I2) = R0
0825F  DM(I2,M2) = R0
08260  BIT SET MODE1 0x00001000                                  ; {IRPTEN}
08261  RTS
```

## 7. Differences between images [V]
* seg_rth identical in all six. puls2os1 == puls2os3 (seg_init/seg_pmco).
* DSP0 only: EPB1 kick `[0,EOM]` + MSGR5=0 at init, and backplate read (FLAG11 strobe, IOSTAT → `backplateID`).
* DSP1-5: no kick, MSGR5 left at 1, EOM layout shifted one word (finish_eom+1 = 0xC50A, sync_eom 0xC508 = R6
  without bit 18).
* DSP2: MODE2 |= FLG3O and ASTAT bit 22 (= FLAG3 output high) [V]; purpose unknown [?]. DSP4: sync-plate detect + `timerInterrupt`,
  `setSyncPlate`, `enable/disableXTC`. DSP5: µC interface (`ucInit`, IRQ0 → `ucR`, SPORT1 MIDI/data `ucSpr1Asserted`).
  Its pmco is shifted by 1 word (os_firstsync 0x81A4, os_epb1_int 0x81B6, updateTCBCounter 0x8247).
* `sysMsg` 0xC419, `dspID` 0xC409, `dspAckDest` 0xC40A, `cmdMask` 0xC408, `numDSP` 0xC40C, `loadPX1` 0x80D6: same in all six.

## 8. Tool usage
```
python3 tools/sharc_dis.py scope_full/app/App/Dsp/puls2os0.21k                 # all PM sections
python3 tools/sharc_dis.py puls2os0.21k --section seg_init
python3 tools/sharc_dis.py puls2os0.21k --from 0x8080 --to 0x809c
python3 tools/sharc_dis.py puls2os0.21k --section seg_dmda                    # DM words + symbols
python3 tools/sharc_dis.py --word 063E00008080 0B3E00000000 --pc 0x8005
```
Validation: the known words decode as expected (0x063E00008080 = JUMP 0x8080, 0x0A3E.. = RTS, 0x0B3E.. = RTI,
0x06BE.. = CALL). In all six images every instruction decodes without an unknown opcode, and every static branch
or loop target lands inside a PM section (77-92 targets per image). Every vector slot is RTI or a JUMP to code,
and every function ends in RTS/RTI or a JUMP.
