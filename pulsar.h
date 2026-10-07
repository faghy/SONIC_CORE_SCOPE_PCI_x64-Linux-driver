/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Linux ALSA Driver for Creamware / Sonic Core Pulsar 2 & Scope DSP Cards
 * Reverse engineered from scScope.sys (Windows x64 WDM driver).
 * See ../re_notes/scScope_sys.md for the evidence behind each register.
 */

#ifndef _PULSAR_H_
#define _PULSAR_H_

#include <linux/types.h>
#include <linux/pci.h>
#include <linux/interrupt.h>
#include <sound/core.h>
#include <sound/pcm.h>
#include <sound/hwdep.h>

#include "pulsar_uapi.h"

#define DRV_NAME "snd-pulsar"
#define DRV_DESC "Creamware / Sonic Core Scope & Pulsar II ALSA Driver"

/* PCI Vendor and Device IDs (from scopewdm64.inf) */
#define PCI_VENDOR_ID_CREAMWARE        0x14b5

#define PCI_DEVICE_ID_SCOPE_SP         0x0200
#define PCI_DEVICE_ID_PULSAR1          0x0300
#define PCI_DEVICE_ID_PULSAR_SRB       0x0400
#define PCI_DEVICE_ID_PULSAR2          0x0600
#define PCI_DEVICE_ID_SCOPE_PRO_8      0x0800
#define PCI_DEVICE_ID_SCOPE_PRO_9      0x0900
#define PCI_DEVICE_ID_SCOPE_PRO_A      0x0a00
#define PCI_DEVICE_ID_SCOPE_PRO_B      0x0b00

/* BAR 0: 4 MB non-prefetchable MMIO */
#define PULSAR_BAR_SIZE                (4 * 1024 * 1024)

/* Registers (offsets in BAR 0) */
#define PULSAR_REG_BOARD_ID            0x000000  /* R: board id, rev = (v >> 8) & 0x1f */
#define PULSAR_REG_BOARD_REV_MASK      0x1f
#define PULSAR_REG_BOARD_REV_SHIFT     8
#define PULSAR_REG_INT_STATUS          0x000004  /* R: IRQ status */
#define PULSAR_REG_FIFO_RD             0x000008  /* R: card read index of command FIFO */
#define PULSAR_REG_FIFO_WR             0x00000c  /* W: host write index of command FIFO */
#define PULSAR_REG_SAMPLE_COUNT        0x000010  /* R: free-running sample counter */
#define PULSAR_REG_INT_ACK             0x00001c  /* W: write 0 to acknowledge IRQ */
#define PULSAR_REG_SLOT_BANK           0x000020  /* W: active slot-table bank */

/* Shared SRAM windows */
#define PULSAR_SRAM_SLOTS_A            0x080000  /* slot table bank A / shared SRAM */
#define PULSAR_SRAM_SLOTS_B            0x080800  /* slot table bank B */
#define PULSAR_CMD_FIFO                0x081000  /* 1024-dword host->DSP command ring (rev >= 2) */
#define PULSAR_CMD_FIFO_LEN            1024

/*
 * The interrupt is ours iff (status & 3) != 0 AND (status & 0xffff0000) == 0
 * (scScope.sys ISR @ 0x180007090).
 */
#define PULSAR_INT_PENDING_MASK        0x00000003
#define PULSAR_INT_INVALID_MASK        0xffff0000

/* Host<->DSP audio slots (docs/pcm_streaming.md 2) */
#define PULSAR_SLOT_A(s)               (0x080000 + 4 * (s))   /* slot table bank A */
#define PULSAR_SLOT_B(s)               (0x080800 + 4 * (s))   /* slot table bank B */
#define PULSAR_SLOT_WIN(s)             (0x080000 + 0x200 * (s)) /* per-slot prefetch window */
#define PULSAR_SLOT_IDLE               0x800
#define PULSAR_SLOT_CAPTURE            0x1
#define PULSAR_SLOT_FIRST              0x10
#define PULSAR_SLOT_PC_FIRST           0x180
#define PULSAR_SLOT_MAX                0x1ff
#define PULSAR_RING_FRAMES             0x1000                  /* 16 KB ring per channel */
#define PULSAR_RING_BYTES              (PULSAR_RING_FRAMES * 4)
#define PULSAR_COUNTER_MASK            (PULSAR_RING_FRAMES - 1)

struct pulsar_stream {
	struct snd_pcm_substream *substream;
	const u16 *slots;
	unsigned int channels;
	bool armed;                       /* enable slots at the next ring wrap */
	bool running;                     /* slots live, periods are being reported */
	bool live;                        /* slot entries point at our ring */
};

struct pulsar_card {
	struct pci_dev *pci;
	struct snd_card *card;
	struct snd_pcm *pcm;
	struct snd_hwdep *hwdep;
	struct pulsar_stream playback;
	struct pulsar_stream capture;
	struct pulsar_pcm_route route;
	bool route_valid;
	struct mutex route_mutex;

	void __iomem *iobase;
	resource_size_t iobase_phys;
	resource_size_t iobase_len;

	u32 board_raw_id;
	u8 board_rev;
	int irq;

	/* IRQ statistics, exported via hwdep for bring-up debugging */
	atomic_t irq_count;
	u32 last_int_status;

	spinlock_t reg_lock;
};

int pulsar_pcm_set_route(struct pulsar_card *chip, const struct pulsar_pcm_route *r);
void pulsar_pcm_interrupt(struct pulsar_card *chip);
void pulsar_pcm_quiesce(struct pulsar_card *chip);
int pulsar_hwdep_create(struct pulsar_card *chip);

#endif /* _PULSAR_H_ */
