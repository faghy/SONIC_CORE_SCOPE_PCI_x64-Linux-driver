// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Creamware / Sonic Core Pulsar 2: host -> DSP messages through the command FIFO
 * (scScope.sys SendMsgBuf @0x18000f570, Sim2k sendMsg FUN_10c33140; docs/boot_sequence.md 3.6).
 *
 * After boot the userspace loader is done with the FIFO and the kernel may use it for runtime
 * SetValue (mixer controls). The write index is re-read from the card each time, so occasional
 * debugging writes from userspace (pulsar_loader.py peek) do not desynchronise it.
 */

#include <linux/io.h>
#include <linux/delay.h>
#include <linux/jiffies.h>

#include "pulsar.h"

#define FIFO_LEN		PULSAR_CMD_FIFO_LEN
#define FIFO_HEADROOM		0x100

static int fifo_send(struct pulsar_card *chip, const u32 *w, unsigned int n)
{
	unsigned long timeout = jiffies + msecs_to_jiffies(100);
	u32 wr = readl(chip->iobase + PULSAR_REG_FIFO_WR) & (FIFO_LEN - 1);
	unsigned int i;

	for (;;) {
		u32 rd = readl(chip->iobase + PULSAR_REG_FIFO_RD) & (FIFO_LEN - 1);

		if (FIFO_LEN - ((wr - rd) & (FIFO_LEN - 1)) >= n + FIFO_HEADROOM)
			break;
		if (time_after(jiffies, timeout))
			return -ETIMEDOUT;
		usleep_range(100, 200);
	}
	for (i = 0; i < n; i++) {
		writel(w[i], chip->iobase + PULSAR_CMD_FIFO + 4 * wr);
		wr = (wr + 1) & (FIFO_LEN - 1);
	}
	writel(wr, chip->iobase + PULSAR_REG_FIFO_WR);
	return 0;
}

/* SetValue on a DSP whose OS is running (state 2): one wrapped single-word frame. */
int pulsar_dsp_set_value(struct pulsar_card *chip, unsigned int dsp, u32 addr, u32 val)
{
	u32 t = (dsp & 0xf) << 21;
	u32 frame[] = {
		t | 0x120000, t | 0x120000,
		((dsp | 0x10) << 21) | (addr & 0x1fffff) | 0x20000000, val,
		t | 0x20100000,
		0x0fe0c008, 0x1212, 0x2424, 0x3636, 0x4848, 0x5a5a, 0x6c6c, 0x7e7e,
		0,
	};
	int err;

	mutex_lock(&chip->fifo_mutex);
	err = fifo_send(chip, frame, ARRAY_SIZE(frame));
	mutex_unlock(&chip->fifo_mutex);
	return err;
}
