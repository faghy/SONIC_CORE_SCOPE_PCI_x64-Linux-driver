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

struct pulsar_card {
	struct pci_dev *pci;
	struct snd_card *card;
	struct snd_pcm *pcm;
	struct snd_hwdep *hwdep;
	struct snd_pcm_substream *playback_substream;
	struct snd_pcm_substream *capture_substream;

	void __iomem *iobase;
	resource_size_t iobase_phys;
	resource_size_t iobase_len;

	u32 board_raw_id;
	u8 board_rev;
	int irq;

	/* IRQ statistics, exported via hwdep for bring-up debugging */
	atomic_t irq_count;
	u32 last_int_status;

	atomic_t current_period;
	spinlock_t reg_lock;
};

int pulsar_pcm_create(struct pulsar_card *chip);
void pulsar_pcm_period_elapsed(struct pulsar_card *chip);
int pulsar_hwdep_create(struct pulsar_card *chip);

#endif /* _PULSAR_H_ */
