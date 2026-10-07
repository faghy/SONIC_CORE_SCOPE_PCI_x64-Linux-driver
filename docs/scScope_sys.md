# scScope.sys (Windows x64, WDM/PortCls) — reverse-engineering notes

Source: `decompiled/scScope.sys.c` (Ghidra) cross-checked with objdump
(`scScope.asm`, image base 0x180000000). Every claim cites the function
address. Confidence tags: **[C]** confirmed from code (asm-checked where noted),
**[L]** likely / strong inference, **[?]** uncertain / speculation.

Driver version reported to userspace: `0x50000` (5.0.0), IOCTL 0x1d2030 @0x1800099d0. [C]

---------------------------------------------------------------------------
## 0. TL;DR — most important facts

1. **The kernel driver never boots the DSPs.** No code path in scScope.sys
   uploads DSP code or toggles a DSP reset, except `BAR[0x00] = 0xFF` on last
   close (registry `ResetIfIdle`, default 1) @0x1800094c0. Boot is done by
   userspace (Sim2k.dll) via (a) raw BAR peek/poke IOCTLs, (b) mapping the
   whole 4 MB BAR into the process (IOCTL 0x1d2048), (c) raw dword writes into
   the host→card command FIFO (IOCTL 0x1d20a4). [C]
2. **ISR** @0x180007090: read `BAR+0x10` (value discarded), write `0` to
   `BAR+0x1C` (ack, *before* status read), read `BAR+0x04`. Ours iff
   `(st & 3) != 0 && (st & 0xFFFF0000) == 0`. Status ORed into ctx+0xB0, DPC
   queued. [C, asm-checked]
3. **Host→card command FIFO** (rev ≥ 2 boards): 1024 dwords at
   `BAR+0x81000`, card read index at `BAR+0x08` (R), host write index published
   to `BAR+0x0C` (W). SendMsgBuf @0x18000f570. [C]
4. **Audio streaming = DSP-side bus-master from host memory.** Each "slot"
   (channel) gets a physically contiguous host buffer (16 KB = 0x1000 32-bit
   samples, or 128 KB) from `MmAllocateContiguousMemorySpecifyCache(<4 GB,
   boundary=size, MmCached)`. The 32-bit physical address (| flag bits) is
   written into a **slot table in BAR SRAM at BAR+0x80000 + slot*4 (bank A) or
   BAR+0x80800 + slot*4 (bank B)**; `BAR+0x20` selects the active bank. The
   driver never programs a DMA engine; the loaded DSP firmware consumes the
   table. [C for the writes, L for interpretation]
5. **Play/record position** = free-running sample counter `BAR+0x10`
   (masked with ring size−1, e.g. `& 0xFFF`). Block size ctx+0x9C (default
   0x400, settable 0x40..0x400 pow2). [C]
6. `DIOC_SCOPE_CONFIG_SPI` (0x1d2118) / `scScopeSendMsgSPI` is used by Sim2k's
   **ScopeXite** class ("ScopeXite%d::spiConfigDriver ()") — i.e. Xite boards.
   For Pulsar II the SPI mask (ctx+0x1F0) is presumably 0 and every DSP write
   goes through the plain FIFO frames (0x18000fa10 / 0x18000fb70). [L]
7. Corrections to CLAUDE.md: the IRQ "ours" test is AND not OR; 0x10 is the
   sample clock, not an "event counter"; 0x80000 is not "the mailbox FIFO"
   (the FIFO is at 0x81000); the `(cmd<<26)|(len<<22)|0xA0000|addr` header is
   only the *SPI* (Xite) packet header, written into DSP memory via the FIFO,
   never directly to MMIO.

---------------------------------------------------------------------------
## 1. Card context (allocated by initScope @0x18001ea90, 0x5AE8 bytes, tag 'INIT')

Created from the PnP resource list in StartDevice2 @0x180032e30.

| ctx off | init value | meaning | evidence |
|---|---|---|---|
| +0x000 | idx+1 | card id used in every IOCTL (input dword 0) | @0x18001ea90, lookup @0x1800090a0 |
| +0x004 | IRQ vector | from CM descriptor type 2 | @0x18001ea90 |
| +0x008/+0x00C | phys start / end of BAR | MmUnmapIoSpace len @0x18001f110 |
| +0x020 | 0 | open count (IOCTL 0x1d2028/0x1d202c) | @0x1800099d0 |
| +0x024 | `(readl(BAR+0)>>8)&0x1F` | board revision/ID ("rev"); code branches on `rev < 2` | @0x18001ea90, @0x1800101d0, @0x180010960 |
| +0x048 | MmMapIoSpace(BAR0, len, MmNonCached) | BAR virtual base | @0x18001ea90 |
| +0x050 | | IRQ status snapshot used by DPC | @0x180007720 |
| +0x064 | | event flags (|1 rate change, |2/|4 ASIO, |8, |0x10) reported to user | @0x1800191d0, 0x1800146e0 |
| +0x070..+0x078 | 0x78 = 0x400 | ctx+0x78 = "wclk" running sample clock | @0x18001ea90, 0x180012f00 |
| +0x07C | 48000 | nominal sample rate (bookkeeping only) | @0x1800191d0 |
| +0x080 | 1 | | |
| +0x084 | idx | card index in global table DAT_180027400[] | |
| +0x088 | 0 | "master" card pointer (clock-slave linking, IOCTL 0x1d2080) | |
| +0x098 | 0x40 | min block / PIO half-buffer size | |
| +0x09C | 0x400 | block size (samples) | IOCTL 0x1d20c4 |
| +0x0A0 | 0x3F | mask for +0x98 | |
| +0x0A4 | 0x3FF | mask for +0x9C | |
| +0x0B0 | | ISR→DPC accumulated IRQ status (xchg) | @0x180007090/0x180007720 |
| +0x0B8 | 0x444 | | |
| +0x0CC..+0x1DF | | internal FIFO ring-state struct (0x114 bytes) when IOCTL 0x1d20ac used | |
| +0x1E0 | | user VA of mapped external ring-state (IOCTL 0x1d2060) | @0x180016ab0 |
| +0x1E8 | | **pointer to ring-state struct** (either ctx+0xCC or mapped user mem) | |
| +0x1F0 | | SPI-enabled DSP-target bitmask | IOCTL 0x1d2118 |
| +0x1F4..+0x20B | | SPI ring descriptor (24 bytes, from user) | IOCTL 0x1d2118, @0x18000fe60 |
| +0x274 | 0x200 | max slots (512) | |
| +0x278 + s*4 | | user descriptor (32-bit user VA) of slot s | @0x180015260 |
| +0xA78 + s*8 | | kernel mapping of slot descriptor | |
| +0x1A78 + s*8 | | **physical address** of slot s host buffer | |
| +0x2A78 + s*8 | | kernel VA of slot s host buffer | |
| +0x3A78 | −1 | highest slot in use | |
| +0x3A7C | 0 | "slot table dirty" → rebuilt in DPC by 0x180010960 | |
| +0x3A80 | | current table bank (0/1) | @0x180010960 |
| +0x3A88 | | last processed block start | @0x180012f00 |
| +0x3A8C | 0x30 | | |
| +0x3AA8/+0x3AAC | | heartbeat DSP address / counter (IOCTL 0x1d20c0) | @0x180012f00 |
| +0x3AB0 | | event clock | |
| +0x3AB4/8/C | | DPC latency stats (cur/avg/max) | @0x180012f00 |
| +0x3AD4/+0x3AD8[] | | host-client (ASIO-like) objects | @0x18001c140 |
| +0x5AD8 | | client list head | |
| +0x5AE0 | | 0x8000-byte pool buffer (IOCTL 0x1d20a0) | |

A pseudo-card `DAT_180027490` (id −1) is used as a global/virtual card.

---------------------------------------------------------------------------
## 2. BAR0 map (4 MB MMIO)

Found by a register-taint scan of the whole asm (every `mov 0x48(%r),%r`
propagated through stack spills) plus manual review of computed offsets.
R/W is from the host driver's perspective.

| Offset | Dir | Meaning | Functions |
|---|---|---|---|
| 0x000000 | R | Board ID: `(v>>8)&0x1F` → ctx+0x24 | 0x18001ea90 (asm 0x18001ee17); Sim2k reads it too |
| 0x000000 | W | `0xFF` on last close when `ResetIfIdle`≠0 → board/DSP reset [L] | 0x1800094c0 (asm 0x1800095e6) |
| 0x000004 | R | IRQ status. Ours iff `(v&3)!=0 && (v&0xFFFF0000)==0` | ISR 0x180007090 |
| 0x000004 | W | `0` on last close (IRQ disable? [L]) | 0x1800094c0 |
| 0x000008 | R | rev≥2: card read index of FIFO @0x81000 (`&0x3FF`). rev<2: read index of pair FIFO | 0x18000f570, 0x18000f8e0 |
| 0x00000C | W | rev≥2: host write index of FIFO @0x81000 | 0x18000f570 |
| 0x00000C | R/W | rev<2 only: write index of pair FIFO (R); also written with channel count in 0x180010960 [?] | 0x18000f8e0, 0x180010960 |
| 0x000010 | R | **free-running sample counter** (wclk / DMA position). Read at ISR entry (discarded), each DPC, all position queries | 0x180007090, 0x180012f00, 0x180007720, 0x1800091e0, 0x180017290, … |
| 0x00001C | W | IRQ ack: write `0`, done **before** reading 0x04 | 0x180007090 (asm 0x1800070ea) |
| 0x000020 | W | active slot-table bank (0 → 0x80000, 1 → 0x80800); 0 when slaved | 0x180010960 |
| 0x050000 + i*4 | W | rev<2 pair FIFO: address word (4096 entries) | 0x18000f8e0 |
| 0x0D0000 + i*4 | W | rev<2 pair FIFO: data word | 0x18000f8e0 |
| 0x080000 | R/W | slot-table control word: `(w & 0xC03FFFFF) | (n&0x1F)<<25 | (n&0xE0)<<17`; low 10 bits read as `base*2` | 0x180010960 |
| 0x080004 | W | control word + 1 | 0x180010960 |
| 0x080000 + s*4 (s≥0x10) | W | **slot table bank A**: phys addr \| flags of slot s | 0x180010960, 0x180013c80, 0x180014420, 0x180016d90 |
| 0x080800 + s*4 | W | slot table bank B (same encoding) | same |
| 0x080040 / 0x080840 | W | cleared to 0 on last close (slot 0x10 entry, both banks) | 0x1800094c0 |
| 0x080000 + s*0x200 (0x80 dwords) | W | alternative per-slot PIO double buffer (2×ctx+0x98 = 2×0x40 samples) for slot types 0x1A / wave-out PIO; cleared on slot (re)start; copied when moving slots | 0x1800146e0, 0x180012a60, 0x180017700, 0x180015f70. **Overlaps the table above** → only one model can be active per firmware [?] |
| 0x080000 + k*4 | R | SPI: remote ring read pointer (k = ctx+0x208) | 0x18000fe60 |
| 0x080000 + p*4 | — | client parameter pointers handed to host clients (IOCTL 0x1d20c8) | 0x18001d600 |
| BAR + off*4 (user-configured) | R | MIDI/parameter mailboxes; offsets come from user slot descriptors (+0xDE,+0xE4,+0xF6, 7 pairs at 0x180001dc0) | 0x180001dc0, 0x180012470, 0x1800184d0, 0x180011bd0 |
| 0x081000 + i*4 | W | **host→card command FIFO**, 1024 dwords | 0x18000f570 |
| any | R/W | raw peek/poke/copy/fill + full 4 MB user mapping via IOCTLs | see §5 |

Nothing in the driver ever writes 0x14/0x18 or reads 0x1C. Sim2k.dll (not the
driver) writes BAR dwords 0..8 at startup — see §7.

---------------------------------------------------------------------------
## 3. Interrupt handling

Registration (CAdapterCommon::Init @0x1800343b0):
`KeInitializeDpc(&DeviceObject->Dpc, DPC=0x180007720, DeviceObject)`,
`KeSetImportanceDpc(High)`, `PcNewInterruptSync(&is, NULL, ResourceList, 0, InterruptSyncModeNormal=1)`,
`is->RegisterServiceRoutine(ISR=0x180007090, ctxCommon, FALSE)`, `is->Connect()`.
CAdapterCommon+0x288 = card ctx, +0x278 = DeviceObject, +0x290 = DPC spinlock.

### ISR @0x180007090 [C, asm verified]
```c
NTSTATUS isr(PINTERRUPTSYNC is, CAdapterCommon *cc) {
    card *c = cc->card;                     /* cc+0x288 */
    if (!c) return STATUS_UNSUCCESSFUL;
    void __iomem *bar = c->bar;
    (void)readl(bar + 0x10);                /* dead read (asm 0x1800070de) */
    writel(0, bar + 0x1C);                  /* ack FIRST */
    u32 st = readl(bar + 0x04);
    if ((st & 3) == 0 || (st & 0xFFFF0000) != 0) {
        DbgPrint("ISR not from CW HW\n");
        return STATUS_UNSUCCESSFUL;         /* not ours */
    }
    if (g_isr_busy) { DbgPrint("2nd ISR ignored\n"); return STATUS_SUCCESS; }
    g_isr_busy = 1;
    c->irq_status |= st;                    /* ctx+0xB0 */
    KeInsertQueueDpc(&cc->DeviceObject->Dpc, is, cc);
    g_isr_busy = 0;
    return STATUS_SUCCESS;
}
```
Note: status is never written back; the only ack is `BAR+0x1C = 0`. Bits:
bit0 → wave service in DPC; bit1 meaning unknown [?].

### DPC @0x180007720 [C]
```c
void dpc(..., CAdapterCommon *cc /*R9*/) {
    card *c = cc->card;
    if (g_dpc_busy) { DbgPrint("2nd DPC ignored\n"); return; }
    g_dpc_busy = 1; c->+0xB4 = 0;
    KeAcquireSpinLockAtDpcLevel(&cc->lock);           /* cc+0x290 */
    for (;;) {
        u32 st = xchg(&c->irq_status, 0);             /* ctx+0xB0 */
        c->+0x50 = st; if (!st) break;
        block_process(c);                             /* 0x180012f00 */
        midi_in_poll();                               /* 0x18000c990 */
        if (master(c) == current_master() && (st & 1))
            wave_service_all();                       /* 0x18000f270 -> 0x18000eb70 per stream */
    }
    KeReleaseSpinLockFromDpcLevel(&cc->lock);
    g_dpc_busy--;
    /* user notification events (IOCTL 0x1d2190/0x1d219c) */
    if (evt1 && (readl(bar+0x10) & ~c->blkmask) != last1) { last1 = ...; KeSetEvent(evt1); cnt1++; }
    if (c->+0x60 && !c->master && c->+0x58) {
        u32 p = readl(bar+0x10) & ~c->blkmask;
        if (p != last2) { val = (readl(bar+0x10) & ~0x3F) | c->flags64; c->flags64 = 0; KeSetEvent(evt2); }
    }
}
```

### block_process @0x180012f00 [C]
```c
pos0 = readl(bar+0x10);
... client callbacks (0x180012470 per card, 0x18001e740 clients) ...
if (work) { p = readl(bar+0x10) & 0xFFF; copy/convert between client and slot buffers (0x180012da0) ... }
cur  = readl(bar+0x10);
prev = (cur - blk) & ~(blk-1);  next = (cur + blk) & ~(blk-1);
if (prev != c->last_block /*+0x3A88*/) {
    for each linked card: wclk(+0x78) += (prev - last_block) & 0xFFF;
                          slot_service(card, prev, next, -1);   /* 0x180011bd0: per-slot copy,
                                                                   rebuilds slot table if dirty (0x180010960) */
    client_block_cb(c, next, blk);                              /* 0x18001e280 */
    0x1800119f0(...);
    stats: (readl(bar+0x10) - pos0) & 0x7FFF -> +0x3AB4 (cur), +0x3ABC (max), +0x3AB8 (avg)
    c->last_block = prev;
}
for each card with heartbeat addr (+0x3AA8): SetValue(card, addr, ++counter);   /* 0x1800101d0 */
```

---------------------------------------------------------------------------
## 4. Bring-up sequence (everything done to hardware)

DriverEntry (@0x1800208c0 region, decomp line ~16590): reads registry
(@0x180032280: `WaveMaxChannels`, `Preferred Bit Depth`(16), `Output Preload`(50),
`AllowStandby`(0), `ResetIfIdle`(1), `PCI Latency Timer`(0)), PcInitializeAdapterDriver,
hooks IRP_MJ_CREATE/CLOSE/DEVICE_CONTROL/WRITE/PNP/POWER/SYSTEM_CONTROL.

AddDevice @0x1800323b0: control device `\Device\scScope` (type 0x1D),
symlink `\DosDevices\scscope`; PcAddAdapterDevice(StartDevice2). No HW access.

StartDevice2 @0x180032e30, in order:
1. Split resources (@0x180033b50).
2. **initScope @0x18001ea90**: alloc ctx, `bar = MmMapIoSpace(BAR0, len, MmNonCached)`,
   **single MMIO access: `rev = (readl(bar+0) >> 8) & 0x1F`**, set defaults (table §1).
3. **PCI config space** via IRP_MN_READ_CONFIG/WRITE_CONFIG (@0x180007550):
   read 16 bytes at cfg offset 0; then write cfg byte **0x0D (Latency Timer)** =
   registry `PCI Latency Timer` if set (≠0, ≠−1), else **0x80 if PCI Revision ID < 2, else 0x60**.
   (Pulsar2 rev 02 → 0x60.) [C, asm: movzbl of 0x180025600]
4. CAdapterCommon::Init (@0x1800343b0): DPC + interrupt sync + ISR connect (§3).
5. PcRegisterAdapterPowerManagement, install Wave01..WaveNN (WavePci) and MIDI
   subdevices, physical connections, device interface.

**No delays, no polling, no register writes, no DSP reset/boot.** The card is
left exactly as found until userspace opens it.

Open (IOCTL 0x1d2028) — software only (§5). Close of last handle
(IOCTL 0x1d202c or process exit, @0x1800094c0):
```c
writel(0, bar + 0x80040); writel(0, bar + 0x80840);   /* slot 0x10 entries, both banks */
writel(0, bar + 0x04);
if (ResetIfIdle) writel(0xFF, bar + 0x00);              /* reset board */
```
plus freeing slot buffers, unmapping the user BAR mapping, etc.

---------------------------------------------------------------------------
## 5. IOCTL interface

Control device `\\.\scScope` (Sim2k also tries `\\.\scScopeXite`, `\\.\cwscope`).
All codes are `CTL_CODE(0x1D, 0x800+n, METHOD_BUFFERED, FILE_ANY_ACCESS)`,
range 0x1d2000..0x1d27ff (@0x180001370). Dispatcher builds
`DIOCParams { [0] EPROCESS*, [1] code, [2] in (Type3InputBuffer = raw user ptr),
[3] in_len, [4] out (SystemBuffer), [5] out_len, [6] 0, [7] FileObject }` and calls
@0x1800099d0. Return 1 → STATUS_SUCCESS with Information = out_len; 0 →
STATUS_INVALID_DEVICE_REQUEST.

**Conventions:** input dword[0] = card id (ctx+0); helper @0x180009120 checks
in/out length and that the card is open. Output dword[0] = status
(0 OK, −1 no such card, −2 busy/fail, −3 not open/alloc fail, −5 bad size/arg,
−10 unsupported); results from out+4. Pointers inside input are **32-bit user
VAs** (Sim2k is 32-bit/WOW64); the driver zero-extends them.

### Hardware-touching / boot-relevant IOCTLs (all @0x1800099d0 unless noted)
| Code | Input (dwords) | Output | Action |
|---|---|---|---|
| 0x1d2028 OPEN | [0]=card id | [1]=card id | open card (count++), reset wclk/stat fields; −2 if already open |
| 0x1d202c CLOSE | [0] | [1]=open count | count--; at 0 → cleanup incl. BAR writes (§4) |
| 0x1d2030 VERSION | – | [1]=0x50000,[2]=0 | Sim2k requires 0x50000 |
| **0x1d2034 PEEK** | [0],[1]=byte off | [1]=`readl(bar+off)` | raw MMIO read |
| **0x1d2038 POKE** | [0],[1]=off,[2]=val | – | `writel(val, bar+off)` |
| 0x1d203c | [0] | [1]=ctx+0x24 (board rev) | |
| 0x1d2040 READ BLK | [0],[1]=off,[2]=user dst,[3]=len | – | byte-copy `bar+off` → user (len&~3) |
| 0x1d2044 WRITE BLK | [0],[1]=off,[2]=user src,[3]=len | – | byte-copy user → `bar+off` |
| **0x1d2048 MAP BAR** | [0] | [1]=user VA (32-bit) | IoAllocateMdl(bar, 0x400000) + MmBuildMdlForNonPagedPool + MmMapLockedPages(UserMode) — whole 4 MB BAR mapped into caller |
| 0x1d20a8 FILL | [0],[1]=off,[2]=val,[3]=len | – | dword fill of BAR range (@0x180006120) |
| **0x1d20a4 SENDMSG** | [0],[1]=user ptr,[2]=n dwords | – | SendMsgBuf(ctx, ptr, n) @0x18000f570 |
| **0x1d20ac RING INIT** | [0],[1]=initial write idx | – | ring = ctx+0xCC, zero 0x114 B, ring.wr=[1], ring.prefix_done=1 |
| 0x1d2060 RING MAP | [0],[1]=user VA | – | lock/map 0x114-byte user ring-state as ctx+0x1E8 (tag 'FIFO') — not used by Sim2k |
| 0x1d2064 | [0] | – | unmap it |
| **0x1d2118 CONFIG_SPI** | [0],[1]=target mask,[2]=user ptr to 24 B | – | ctx+0x1F0=mask; ctx+0x1F4..0x20B = {base, size, rdcache, wr, doorbell_addr, bar_idx} |
| 0x1d20c0 HEARTBEAT | [0],[1]=DSP addr | – | DPC sends SetValue(addr, ++n) every block (0 disables) |
| 0x1d20c4 BLOCKSIZE | [0],[1]=n | – | n pow2, 0x40≤n≤0x400 → ctx+0x9C, mask +0xA4 (all cards if id −1) |
| 0x1d20cc CLK RESET | [0] | – | wclk=blk, last_block=−blk&0xFFF |
| 0x1d20e0 CLK ADV | [0],[1]=pos,[2]=n | – | client callbacks + wclk+=n |
| 0x1d2074 SRATE | [0],[1]=Hz (0 or 12000..96000) | – | bookkeeping only (ctx+0x7C, timing doubles); clears ring stall flag. **No HW access** (@0x1800191d0) |
| 0x1d2078 | [0] | [1]=srate | |
| 0x1d2080 LINK | [0],[1]=master card id | – | ctx+0x88 (clock slave) |
| 0x1d2058 SET SLOT | [0],[1]=slot,[2]=user desc ptr | – | @0x180015260 "Setting buffer": allocate contiguous buffer, record phys/VA, mark table dirty |
| 0x1d205c REL SLOT | [0],[1]=slot,[2]=desc | – | @0x180016320 "Releasing buffer" |
| 0x1d2084 MOVE SLOT | [0],[1]=slot,[2]=dst card,[3]=dst slot | – | @0x180015f70 (copies 0x80 dwords of BAR+0x80000+slot*0x200) |
| 0x1d2068 ALLOC CONTIG | [0],[1]=pages | [1]=kernel VA low32,[2]=phys | MmAllocateContiguousMemorySpecifyCache(pages*4K, ≤4GB, boundary=size, cached) @0x180005fb0 |
| 0x1d206c FREE CONTIG | [0],[1]=VA,[2]=pages | – | (VA truncated to 32 bit — broken on x64 [?]) |
| 0x1d20a0 | [0] | [1..2]=kernel VA | 0x8000-byte pool buffer |
| 0x1d20b8 / 0x1d20bc | [0],[1]=user VA,[2]=len | [1..2]=sys VA | lock+map / unlock user memory |
| 0x1d2088/0x1d208c/0x1d2090/0x1d209c/0x1d20c8/0x1d20b0/0x1d21a4/0x1d2098 | | | host-client ("ASIO-like") object create/destroy/connect/order, param pointers into BAR+0x80000 (@0x18001c140, 0x18001ccf0, 0x18001d260, 0x18001d020, 0x18001d600, 0x18001d7c0, 0x18001e4b0) |
| 0x1d2100/0x1d2104 | [0] | [1]=+0x74,[2]=+0x70,[3]=wclk,[4]=readl(bar+0x10) | consistent snapshot / reset +0x70 |
| 0x1d2190 / 0x1d2198 | event handle / – | [1]=counter | register / **block until** block-change event (DPC) |
| 0x1d219c / 0x1d21a0 | event handle / – | [1]=(BAR[0x10]&~0x3F)\|flags | second event |
| 0x1d2094 | – | [1]=0x80000002 (HKLM), +8 = ANSI registry path | |
| 0x1d20d0/0x1d20d4 | string ptr | | set/get 79-char global string |
| 0x1d20d8 | – | [1]=0x800091e0 (Scope_Control addr low32),[2]=1 | |
| 0x1d20e4 | [0] | [1]=0x1000 or 0x8000 | slot ring length (ctx+0x3AD0 never set → 0x1000) |
| 0x1d20e8/0x1d20ec/0x1d20f0 | | | reread registry / memory stats / return PID |
| 0x1d2070 | | | no-op, always −2 |
| 0x1d20b4 | | −10 | unsupported |
| 0x1d240c..0x1d2454 | | | legacy VxD-compat set (@0x1800066d0; "VAXED.VXD"); 0x1d2440 returns BAR[0x10]; 0x1d2450 returns `(BAR[0x10]&~0x3F)\|flags` |

Codes 0x1d2000..0x1d2024 are referenced by Sim2k but **not handled** here
(fall to default → STATUS_INVALID_DEVICE_REQUEST) — probably for other drivers.

Exported `Scope_Control(code, arg, sub)` @0x180001330 → @0x1800091e0
(kernel-to-kernel): 0x1d208c destroy client, 0x1d20dc block size, 0x1d2100 DPC
load %, 0x1d2108 clock query (sub 0 wclk, 1 event clock, 2 `wclk − blk +
(BAR[0x10] & mask)`, 3 raw `BAR[0x10]`).

### Pseudocode of the HW-touching handlers
```c
case 0x1d2034: c = card(in[0]); out[1] = readl(c->bar + in[1]);            break;
case 0x1d2038: c = card(in[0]); writel(in[2], c->bar + in[1]);            break;
case 0x1d2040: memcpy_bytes((void*)(u64)in[2], c->bar + in[1], in[3] & ~3); break;
case 0x1d2044: memcpy_bytes(c->bar + in[1], (void*)(u64)in[2], in[3] & ~3); break;
case 0x1d20a8: for (p = c->bar+in[1]; p < c->bar+in[1]+(in[3]&~3); p += 4) writel(in[2], p); break;
case 0x1d2048: mdl = IoAllocateMdl(c->bar, 0x400000); MmBuildMdlForNonPagedPool(mdl);
               c->umap = MmMapLockedPages(mdl, UserMode); out[1] = (u32)c->umap; break;
case 0x1d20a4: SendMsgBuf(c, (u32*)(u64)in[1], in[2]); break;
case 0x1d20ac: memset(&c->ring_int, 0, 0x114); c->ring_int.prefix_done = 1;
               c->ring_int.wr = in[1]; c->ring = &c->ring_int; c->ring_user = 0; break;
case 0x1d2118: memcpy(&c->spi, (void*)(u64)in[2], 24); c->spi_mask = in[1]; break;
```

---------------------------------------------------------------------------
## 6. Host → card message paths

### Ring-state struct (pointed to by ctx+0x1E8, 0x114 bytes)
| off | type | meaning |
|---|---|---|
| +0x00 | int | host write index (0..0x3FF) |
| +0x04 | int | cached free space |
| +0x08 | u16 | "sent" flag (set to 1 after every send) |
| +0x0A | u16 | **stalled** flag (set on FIFO timeout; blocks SetValue/param paths; cleared by 0x1d2074) |
| +0x0E | u16 | prefix-already-sent flag |
| +0x10 | int | prefix word count |
| +0x14.. | u32[] | prefix words (flushed before the first message) |

### SendMsgBuf @0x18000f570 [C]
```c
void SendMsgBuf(card *c, const u32 *w, int n) {
    void __iomem *bar = c->bar; ring_t *r = c->ring;
    int need = n + r->pfx_cnt, tries = 0;
    irql = KeAcquireSpinLockRaiseToDpc(&g_fifo_lock);            /* global, all cards */
    if (++g_depth > 1) DbgPrint("recursive call to SendMsgBuf despite cli!");
    if (r->free < need + 0x100) {
        for (;;) {
            int rd = readl(bar + 0x08) & 0x3FF;
            r->free = 0x400 - ((r->wr + 0x400 - rd) & 0x3FF);
            need = n + r->pfx_cnt;
            if (need + 0x100 < r->free) break;
            KeReleaseSpinLock(&g_fifo_lock, irql);
            if (tries > 9999) { r->stalled = 1; g_depth--;
                DbgPrint("cannot send message due to FIFO stall!"); return; }   /* message dropped */
            irql = KeAcquireSpinLockRaiseToDpc(&g_fifo_lock); tries++;
        }
    }
    if (!r->pfx_done) {
        for (i = 0; i < r->pfx_cnt; i++) { writel(r->pfx[i], bar + 0x81000 + r->wr*4); r->wr = (r->wr+1) & 0x3FF; }
        r->pfx_cnt = 0; r->pfx_done = 1;
    }
    for (i = 0; i < n; i++) { writel(w[i], bar + 0x81000 + r->wr*4); r->wr = (r->wr+1) & 0x3FF; }
    r->free -= need;
    writel(r->wr, bar + 0x0C);                                     /* doorbell */
    r->sent = 1; g_depth--;
    KeReleaseSpinLock(&g_fifo_lock, irql);
}
```
Busy-wait has no delay; keeps ≥256 entries of headroom.

### rev<2 pair FIFO @0x18000f8e0 (older boards, not Pulsar II if rev==6)
```c
if (r->free < 0x100) do r->free = 0x1000 - ((readl(bar+0x0C) - readl(bar+0x08)) & 0xFFF); while (r->free <= 0x100);  /* no timeout */
writel(addr, bar + 0x50000 + r->wr*4); writel(val, bar + 0xD0000 + r->wr*4);
r->wr = (r->wr+1) & 0xFFF; r->free--; r->sent = 1;
```

### DSP-memory write frames (go through SendMsgBuf)
`T(a) = a & 0x01E00000` (DSP select, bits 21..24).

Single word, @0x18000fa10 (14 dwords) [C, asm verified]:
```
T|0x120000, T|0x120000, addr|0x20000000, value,
T|0x20100000, 0x0FE0C008, 0x9999,0x8888,0x7777,0x6666,0x5555,0x4444,0x3333, 0
```
Block, @0x18000fb70 (dsp_write_block(c, addr, n, data)): data split into
chunks of ≤20 words; each chunk is sent as two frames (even / odd words) of
≤10 words each:
```
T|0x120000, T|0x120000,
((addr + i + parity) & 0xE1FFFFFF) | (cnt << 25) | 0x20000000,
data[i+parity], data[i+parity+2], ... (cnt words, stride 2 in DSP address space),
T|0x20100000, 0x0FE0C008, 0x9999..0x3333 (7 words), 0
```
[?] Note the inconsistency: the single-word frame encodes cnt=0 in bits 25..28
while the block frame encodes cnt=N; the exact meaning (and why stride 2) is
defined by whatever DSP-side monitor consumes these frames — treat the frames
as opaque and reproduce byte-exactly.

### SetValue dispatcher @0x1800101d0 [C]
```c
void SetValue(card *c, u32 addr, u32 val) {
    if (!c || !c->ring || c->ring->stalled || !c->bar) return;
    if (c->rev < 2)                         pairfifo_write(c, addr, val);      /* 0x18000f8e0 */
    else if (c->spi_mask & (1u << target(addr))) SendMsgSPI(c, addr, 1, &val);
    else                                    dsp_write_word(c, addr, val);      /* 0x18000fa10 */
}
/* target(a) @0x18000f530 = ((a >> 21) & 0xF) | (((a >> 18) & 1) << 4) */
```
Block variant @0x180010330 (uses addr+1, picks fb70 vs SPI the same way).

### SendMsgSPI @0x18000fe60 (Xite path) [C]
```c
struct spi { u32 base, size, rd, wr, doorbell, bar_idx; } *s = &c->spi;  /* ctx+0x1F4 */
void SendMsgSPI(card *c, u32 addr, int len, const u32 *d) {
    u32 tgt = target(addr), base = s->base;
    DbgPrint("scScopeSendMsgSPI (adr=%X)", addr);
    while (len > 0) {
        int n = min(len, 15), tries = 0;
        while (s->size - ((s->wr - s->rd) & (s->size-1)) < n + s->size/2) {
            s->rd = readl(c->bar + 0x80000 + s->bar_idx*4) - (base & 0xFFFF);
            if (tries++ > 999) { DbgPrint("SPI communication stalled!"); return; }
        }
        u32 pkt[16];
        pkt[0] = tgt << 26 | n << 22 | ((addr & 0x1FFFF) + 0xA0000);
        memcpy(&pkt[1], d, n*4);
        if (s->wr + 1 + n > s->size) {                 /* wrap */
            int a = s->size - s->wr;
            dsp_write_block(c, base + s->wr, a, pkt);
            dsp_write_block(c, base, n + 1 - a, pkt + a);
        } else dsp_write_block(c, base + s->wr, n + 1, pkt);
        s->wr = (s->wr + 1 + n) & (s->size - 1);
        dsp_write_word(c, s->doorbell, (base & 0xFFFF) + s->wr);   /* publish write ptr */
        d += n; addr += n; len -= n;
    }
}
```
So the SPI "packet" lives in a DSP-memory ring and is delivered with ordinary
FIFO write frames.

### Card → host path
There is **no card→host message ring in the driver**. The card talks back only via:
- IRQ status bits (BAR+0x04) and the sample counter BAR+0x10;
- MIDI-in and other "input" slots: the DSP writes into host slot buffers
  (MIDI words `0x00SSDD..` decoded @0x18000c4a0) and/or into BAR mailbox words
  whose offsets userspace registers in slot descriptors (polled in the DPC,
  @0x180012470 / 0x1800184d0 / 0x180001dc0);
- SPI read pointer at BAR+0x80000+idx*4.
Userspace can of course read any BAR word directly through the mapping.

---------------------------------------------------------------------------
## 7. Audio data path

### Slot buffers (@0x180015260 "Setting buffer", @0x180005fb0 "AllocateAlignedPages")
- Per slot: `MmAllocateContiguousMemorySpecifyCache(pages*4096, Low=1?, High=0xFFFFFFFF,
  Boundary=pages*4096, MmCached)`; zeroed; phys (low 32 bits) → ctx+0x1A78[s],
  VA → ctx+0x2A78[s]. pages = **4** (ring of 0x1000 32-bit samples) for most types,
  **0x20** (0x8000 samples) for types 8, 0xF (and 0x19/0x1A in large mode, never
  enabled). Descriptor +0x18 = pages*0x400 samples. [C]
- Wave/ASIO/GSIF buffers are cached across close/reopen per (dir, channel)
  (find/free/updateWaveBuffer @0x1800138f0/0x180013a80/0x180013c80,
  Asio @0x180013fd0/0x1800141b0/0x180014420). On update the new phys is written
  into **both** table banks immediately.

### Slot types (descriptor +0x34) [L from usage]
3 = wave in (record), 4 = wave out (play), 5/6 = MIDI in/out, 9/10 = ASIO in/out,
0x11/0x12/0x13 = linked/"tap" slots referencing another card's slot,
0x14/0x15, 0x19/0x1A = host-client streams, 0x1B = GSIF, 0x0B, 0x0E/0x0F/0x10/0x16,
100/0x65 = test patterns.

### Slot-table entry encoding (@0x180010440) [C for values, L for meaning]
```
entry = phys | flags
  phys        : 32-bit physical address of the slot ring (size-aligned)
  bit0        : set for capture-type slots (3, 9, 0xC, 7, 0xB, 0xE, 0x10, 0x14, 0x16, 0x18, 0x66) → card writes to host
  bit1        : set for 0x8000-sample rings (types 8, 0xF; 0x19/0x1A large mode)
  bits 2..13  : start offset (x & 0xFFF)<<2 for linked slots (0x11/0x12/0x13, 0x1B)
  0x800       : idle/unused slot (default)
  0xC00       : type-0 slot (empty table position filled as last_active+0xC00)
```
Wave-out entries are `phys` only while the slot's start countdown (+0xCC, set
to 0x18 on open) is non-zero... otherwise 0x800 [L: playback gated].

### Slot-table rebuild @0x180010960 (rev ≥ 2) [C]
```c
if (!c->table_dirty || !bar) return;
int end = max(c->maxslot + 1, 0x10);
int base = max((readl(bar+0x80000) & 0x3FF) >> 1, 0x10);
int n = max(c->maxslot + 1 - base, 2);
u32 w0 = (readl(bar+0x80000) & 0xC03FFFFF) | (n & 0x1F) << 25 | (n & 0xE0) << 17;
writel(w0, bar+0x80000); writel(w0 + 1, bar+0x80004);
u32 bankoff = c->master ? 0x80000 : (c->bank ? 0x80000 : 0x80800);
writel(0, bar + bankoff + end*4);                         /* terminator */
for (s = 0x10; s < end; s++) writel(entry(s), bar + bankoff + s*4);   /* empty → prev_used+0xC00 */
if (!c->master) { c->bank ^= 1; writel(c->bank, bar + 0x20); } else writel(0, bar + 0x20);
c->table_dirty = 0;
```
(rev < 2: just `writel(maxslot+1 - nslots/2 or 0, bar+0x0C)`.)

### Positions / timing
- `BAR+0x10` = sample counter. Ring index = `BAR[0x10] & (ring_len-1)`
  (0xFFF for 16 KB rings). Block boundary = `BAR[0x10] & ~(blk-1)`, blk = ctx+0x9C (default 1024).
- The driver assumes one IRQ per block (it processes "prev block" each DPC) but
  never programs the IRQ rate; the rate/clock is configured by the DSP
  firmware/userspace. [L]
- WavePci stream (@0x18000eb70 service, @0x18000e870 copy, @0x18000db40
  mappings): copies IRP mappings into slot rings at
  `(BAR[0x10] & (len-1)) + preload`; GetPosition (@0x18000d810) = frames copied × block-align
  (software), NormalizePhysicalPosition (@0x18000d8d0) = pos/blockalign*1e7/rate.
- Slot sample format: one 32-bit word per sample, MSB-justified.
  16-bit → `s<<16` (or two 16-bit channels packed per word, hi/lo);
  24-bit → `x & 0xFFFFFF00`; see converters @0x18000cc30..0x18000d540. Exact
  packing order for 2-ch/word modes [?].
- Sample rate is **not** set in hardware by the driver (IOCTL 0x1d2074 only
  stores it); rate/clock are DSP-firmware business.

---------------------------------------------------------------------------
## 8. What userspace (Sim2k.dll) does — pointers for the next step

Only lightly checked (decompiled/Sim2k.dll.c), but relevant because the boot is there:
- Card open @FUN_10c21a50 (decomp ~line 26400): 0x1d2030 (version must be 0x50000),
  0x1d2028 (open), **0x1d2048 (map BAR)**, read BAR[0] → boardID check,
  then via the mapping (vtable slot +0x1C4 = `FUN_10c20420` = `BAR[i] = v`,
  +0x120 = `FUN_10c20410` read; confirmed by identical slot spacing 0xA4 in all
  10 vtables of Sim2k.dll):
  ```
  for i in 0x20000..0x203FF: BAR[i] = 0        /* clears BAR+0x80000..0x80FFF */
  BAR[0]=0x2E; BAR[1]=0; BAR[2]=0; BAR[3]=0; BAR[4]=0x14; BAR[5]=4; BAR[6]=0x14; BAR[8]=0;
  if (x != -1) BAR[7]=x;  then vfunc+0x1A0(0)
  ```
  i.e. BAR+0x00/0x04/0x08/0x0C/0x10/0x14/0x18/0x20 have *write* semantics
  (config) different from what the driver reads there. [C for the calls, ? for meaning]
- Probe helper (decomp ~line 26352): uses 0x1d2034/0x1d2038 on BAR+0/+4 for boardID 5.
- 0x1d20ac (internal ring), 0x1d20a4 (raw FIFO sends, with its own frame
  builder and 0x3636/0x4848/0x5a5a/0x6c6c/0x7e7e fill words), 0x1d2118 from
  `ScopeXite::spiConfigDriver`.

The actual DSP code upload / reset-release sequence must therefore be taken
from Sim2k.dll, not from scScope.sys.
