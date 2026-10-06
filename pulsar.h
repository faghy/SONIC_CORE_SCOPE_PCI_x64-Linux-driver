/* SPDX-License-Identifier: GPL-2.0-or-later */
/*
 * Linux ALSA Driver for Creamware / Sonic Core Pulsar 2 & Scope DSP Cards
 * Reverse engineered from scScope.sys (Windows x64 WDM driver)
 */

#ifndef _PULSAR_H_
#define _PULSAR_H_

#include <linux/types.h>
#include <linux/pci.h>
#include <linux/interrupt.h>
#include <linux/ioctl.h>
#include <sound/core.h>
#include <sound/pcm.h>

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

/* MMIO Layout (BAR 0: 4 MB non-prefetchable) */
#define PULSAR_BAR_SIZE                (4 * 1024 * 1024)

/* Register Offsets */
#define PULSAR_REG_BOARD_ID            0x000000  /* Board status & hardware revision */
#define PULSAR_REG_BOARD_REV_MASK      0x1f
#define PULSAR_REG_BOARD_REV_SHIFT     8

#define PULSAR_REG_INT_STATUS          0x000004  /* Interrupt status register */
#define PULSAR_REG_INT_STATUS_MASK     0x00000003
#define PULSAR_REG_INT_COUNT           0x000010  /* Interrupt counter register */
#define PULSAR_REG_INT_ACK             0x00001c  /* Interrupt acknowledge register (write 0 to clear) */

/* DSP Communication & Mailbox / FIFO */
#define PULSAR_MMIO_FIFO_BASE          0x080000  /* 512KB offset in BAR0 */

/*
 * SPI Packet Command encoding:
 * Header = (cmd << 26) | (len << 22) | 0x000a0000 | (addr & 0x1ffff)
 */
#define PULSAR_CMD_SHIFT               26
#define PULSAR_LEN_SHIFT               22
#define PULSAR_MAGIC_PREFIX            0x000a0000
#define PULSAR_ADDR_MASK               0x0001ffff

/* IOCTL definitions matching Windows scScope.sys (Type 0x1d) */
#define PULSAR_IOCTL_MAGIC             'P'
#define PULSAR_IOCTL_RESET             _IO(PULSAR_IOCTL_MAGIC, 0x01)
#define PULSAR_IOCTL_GET_BOARD_REV     _IOR(PULSAR_IOCTL_MAGIC, 0x02, __u32)
#define PULSAR_IOCTL_SEND_DSP_MSG      _IOWR(PULSAR_IOCTL_MAGIC, 0x03, struct pulsar_dsp_msg)

struct pulsar_dsp_msg {
	__u32 cmd;
	__u32 addr;
	__u32 len;
	__u32 data[64];
};

struct pulsar_card {
	struct pci_dev *pci;
	struct snd_card *card;
	struct snd_pcm *pcm;
	struct snd_pcm_substream *playback_substream;
	struct snd_pcm_substream *capture_substream;

	/* MMIO */
	void __iomem *iobase;
	unsigned long iobase_phys;
	unsigned long iobase_len;

	/* Board hardware details */
	u32 board_raw_id;
	u8 board_rev;
	int irq;

	/* Stream position counter */
	atomic_t current_period;

	/* Spinlock for hardware registers */
	spinlock_t reg_lock;

	/* Character device interface */
	struct mutex ioctl_mutex;
};

/* Function prototypes */
int pulsar_pcm_create(struct pulsar_card *chip);
void pulsar_pcm_period_elapsed(struct pulsar_card *chip);

#endif /* _PULSAR_H_ */
