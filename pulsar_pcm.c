// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Creamware / Sonic Core Pulsar 2 ALSA PCM (docs/pcm_streaming.md)
 *
 * Zero-copy: each ALSA channel is one 16 KB ring (0x1000 x 32-bit, MSB-justified). The card's
 * bus-master engine moves one word per slot per sample between the ring and DSP DM 0xC000 + 2*slot;
 * the ring index being played is BAR+0x10 & 0xfff. Which slots carry which channel is decided by the
 * userspace loader (it builds the DSP graph) and passed in with PULSAR_IOCTL_SET_ROUTE; the PCM device
 * is created on the first valid route.
 */

#include <linux/pci.h>
#include <linux/io.h>
#include <linux/delay.h>
#include <sound/core.h>
#include <sound/pcm.h>
#include <sound/pcm_params.h>

#include "pulsar.h"

static void slot_write(struct pulsar_card *chip, unsigned int s, u32 entry)
{
	writel(entry, chip->iobase + PULSAR_SLOT_A(s));
	writel(entry, chip->iobase + PULSAR_SLOT_B(s));
}

static void stream_idle_slots(struct pulsar_card *chip, struct pulsar_stream *st)
{
	unsigned int c;

	for (c = 0; c < st->channels; c++)
		slot_write(chip, st->slots[c], PULSAR_SLOT_IDLE);
	st->armed = st->running = st->live = false;
}

/* Lay out the slot table in both banks (scScope.sys rebuild @0x180010960). No stream may be running. */
static void table_layout(struct pulsar_card *chip)
{
	const struct pulsar_pcm_route *r = &chip->route;
	unsigned int c, s, maxslot = PULSAR_SLOT_FIRST, end, n;
	u32 w0;

	for (c = 0; c < r->play_channels; c++)
		maxslot = max_t(unsigned int, maxslot, r->play_slot[c]);
	for (c = 0; c < r->cap_channels; c++)
		maxslot = max_t(unsigned int, maxslot, r->cap_slot[c]);
	maxslot = max(maxslot, pulsar_delay_max_slot(chip));	/* host delay lines, pulsar_delay.c */
	end = maxslot + 1;
	n = maxslot + 1 > PULSAR_SLOT_PC_FIRST + 2 ? maxslot + 1 - PULSAR_SLOT_PC_FIRST : 2;

	for (s = PULSAR_SLOT_FIRST; s < end; s++)
		slot_write(chip, s, PULSAR_SLOT_IDLE);
	pulsar_delay_restore_entries(chip);		/* delay lines keep running */
	slot_write(chip, end, 0);			/* terminator */
	chip->slot_end = end;

	w0 = readl(chip->iobase + PULSAR_SLOT_A(0));
	w0 = (w0 & 0xc03fffff) | (n & 0x1f) << 25 | (n & 0xe0) << 17;
	writel(w0, chip->iobase + PULSAR_SLOT_A(0));
	writel(w0 + 1, chip->iobase + PULSAR_SLOT_A(1));
}

static const struct snd_pcm_hardware pulsar_pcm_hw = {
	.info = SNDRV_PCM_INFO_MMAP | SNDRV_PCM_INFO_MMAP_VALID |
		SNDRV_PCM_INFO_NONINTERLEAVED | SNDRV_PCM_INFO_BLOCK_TRANSFER,
	.formats = SNDRV_PCM_FMTBIT_S32_LE,
	.rates = SNDRV_PCM_RATE_KNOT,
	.buffer_bytes_max = PULSAR_MAX_CHANNELS * PULSAR_RING_BYTES,
	.period_bytes_min = 64 * 4,
	.period_bytes_max = PULSAR_MAX_CHANNELS * PULSAR_RING_BYTES / 4,
	.periods_min = 4,
	.periods_max = 64,
};

static struct pulsar_stream *to_stream(struct snd_pcm_substream *ss)
{
	struct pulsar_card *chip = snd_pcm_substream_chip(ss);

	return ss->stream == SNDRV_PCM_STREAM_PLAYBACK ? &chip->playback : &chip->capture;
}

static int pulsar_pcm_open(struct snd_pcm_substream *ss)
{
	struct pulsar_card *chip = snd_pcm_substream_chip(ss);
	struct pulsar_stream *st = to_stream(ss);
	struct snd_pcm_runtime *runtime = ss->runtime;
	const struct pulsar_pcm_route *r = &chip->route;
	unsigned int channels;
	int err;

	mutex_lock(&chip->route_mutex);
	channels = ss->stream == SNDRV_PCM_STREAM_PLAYBACK ? r->play_channels : r->cap_channels;
	if (!chip->route_valid || !channels) {
		mutex_unlock(&chip->route_mutex);
		return -ENODEV;
	}
	st->substream = ss;
	st->slots = ss->stream == SNDRV_PCM_STREAM_PLAYBACK ? r->play_slot : r->cap_slot;
	st->channels = channels;
	st->armed = st->running = st->live = false;
	mutex_unlock(&chip->route_mutex);

	runtime->hw = pulsar_pcm_hw;
	runtime->hw.rate_min = runtime->hw.rate_max = r->rate;
	runtime->hw.channels_min = runtime->hw.channels_max = channels;

	err = snd_pcm_hw_constraint_single(runtime, SNDRV_PCM_HW_PARAM_BUFFER_SIZE, PULSAR_RING_FRAMES);
	if (err < 0)
		return err;
	return snd_pcm_hw_constraint_single(runtime, SNDRV_PCM_HW_PARAM_PERIOD_SIZE, r->block);
}

static int pulsar_pcm_close(struct snd_pcm_substream *ss)
{
	struct pulsar_card *chip = snd_pcm_substream_chip(ss);
	struct pulsar_stream *st = to_stream(ss);

	mutex_lock(&chip->route_mutex);
	st->substream = NULL;
	mutex_unlock(&chip->route_mutex);
	return 0;
}

static int pulsar_pcm_hw_params(struct snd_pcm_substream *ss, struct snd_pcm_hw_params *hw)
{
	dma_addr_t addr = ss->runtime->dma_addr;

	/* the slot entry keeps flags in the low 14 bits and the engine is 32-bit */
	if ((addr & (PULSAR_RING_BYTES - 1)) || upper_32_bits(addr)) {
		dev_err(ss->pcm->card->dev, "DMA ring at %pad is not 16 KB aligned below 4 GB\n", &addr);
		return -ENOMEM;
	}
	return 0;
}

static int pulsar_pcm_hw_free(struct snd_pcm_substream *ss)
{
	struct pulsar_card *chip = snd_pcm_substream_chip(ss);
	struct pulsar_stream *st = to_stream(ss);
	unsigned long flags;
	bool was_live;

	spin_lock_irqsave(&chip->reg_lock, flags);
	was_live = st->live;
	stream_idle_slots(chip, st);
	spin_unlock_irqrestore(&chip->reg_lock, flags);

	/* the engine may still prefetch from the ring: wait two blocks before ALSA frees it */
	if (was_live) {
		synchronize_irq(chip->irq);
		msleep(2 * 1000 * chip->route.block / chip->route.rate + 10);
	}
	return 0;
}

static int pulsar_pcm_prepare(struct snd_pcm_substream *ss)
{
	struct pulsar_card *chip = snd_pcm_substream_chip(ss);
	struct pulsar_stream *st = to_stream(ss);
	unsigned long flags;
	unsigned int c;

	spin_lock_irqsave(&chip->reg_lock, flags);
	stream_idle_slots(chip, st);
	for (c = 0; c < st->channels; c++)
		memset_io(chip->iobase + PULSAR_SLOT_WIN(st->slots[c]), 0, 0x200);
	spin_unlock_irqrestore(&chip->reg_lock, flags);
	return 0;
}

static int pulsar_pcm_trigger(struct snd_pcm_substream *ss, int cmd)
{
	struct pulsar_card *chip = snd_pcm_substream_chip(ss);
	struct pulsar_stream *st = to_stream(ss);

	spin_lock(&chip->reg_lock);
	switch (cmd) {
	case SNDRV_PCM_TRIGGER_START:
		st->armed = true;		/* slots go live at the next ring wrap (ISR) */
		break;
	case SNDRV_PCM_TRIGGER_STOP:
		stream_idle_slots(chip, st);
		break;
	default:
		spin_unlock(&chip->reg_lock);
		return -EINVAL;
	}
	spin_unlock(&chip->reg_lock);
	return 0;
}

static snd_pcm_uframes_t pulsar_pcm_pointer(struct snd_pcm_substream *ss)
{
	struct pulsar_card *chip = snd_pcm_substream_chip(ss);

	if (!to_stream(ss)->running)
		return 0;
	return readl(chip->iobase + PULSAR_REG_SAMPLE_COUNT) & PULSAR_COUNTER_MASK;
}

static const struct snd_pcm_ops pulsar_pcm_ops = {
	.open = pulsar_pcm_open,
	.close = pulsar_pcm_close,
	.hw_params = pulsar_pcm_hw_params,
	.hw_free = pulsar_pcm_hw_free,
	.prepare = pulsar_pcm_prepare,
	.trigger = pulsar_pcm_trigger,
	.pointer = pulsar_pcm_pointer,
};

/* Called from the ISR after the IRQ was acknowledged; one IRQ per route.block frames. */
static bool stream_tick(struct pulsar_card *chip, struct pulsar_stream *st, u32 pos, u32 flag)
{
	dma_addr_t base;
	unsigned int c;

	if (!st->substream)
		return false;
	if (st->armed && pos < chip->route.block) {
		/* ring index just wrapped: ALSA buffer offset 0 == ring index 0 */
		base = st->substream->runtime->dma_addr;
		for (c = 0; c < st->channels; c++)
			slot_write(chip, st->slots[c], lower_32_bits(base + c * PULSAR_RING_BYTES) | flag);
		st->armed = false;
		st->running = st->live = true;
		return false;
	}
	return st->running;
}

void pulsar_pcm_interrupt(struct pulsar_card *chip)
{
	struct snd_pcm_substream *play = NULL, *cap = NULL;
	u32 pos;

	spin_lock(&chip->reg_lock);
	pos = readl(chip->iobase + PULSAR_REG_SAMPLE_COUNT) & PULSAR_COUNTER_MASK;
	if (stream_tick(chip, &chip->playback, pos, 0))
		play = chip->playback.substream;
	if (stream_tick(chip, &chip->capture, pos, PULSAR_SLOT_CAPTURE))
		cap = chip->capture.substream;
	spin_unlock(&chip->reg_lock);

	if (play)
		snd_pcm_period_elapsed(play);
	if (cap)
		snd_pcm_period_elapsed(cap);
}

/* Stop the engine touching our rings (remove / shutdown). */
void pulsar_pcm_quiesce(struct pulsar_card *chip)
{
	unsigned long flags;

	spin_lock_irqsave(&chip->reg_lock, flags);
	if (chip->route_valid) {
		stream_idle_slots(chip, &chip->playback);
		stream_idle_slots(chip, &chip->capture);
	}
	writel(0, chip->iobase + PULSAR_REG_INT_STATUS);	/* reg1 = 0: no block IRQs (Windows last close) */
	spin_unlock_irqrestore(&chip->reg_lock, flags);
}

static int pulsar_pcm_create(struct pulsar_card *chip, bool play, bool cap)
{
	struct snd_pcm *pcm;
	int err;

	err = snd_pcm_new(chip->card, "Pulsar PCM", 0, play, cap, &pcm);
	if (err < 0)
		return err;
	pcm->private_data = chip;
	strscpy(pcm->name, "Pulsar2 DSP Audio", sizeof(pcm->name));
	if (play)
		snd_pcm_set_ops(pcm, SNDRV_PCM_STREAM_PLAYBACK, &pulsar_pcm_ops);
	if (cap)
		snd_pcm_set_ops(pcm, SNDRV_PCM_STREAM_CAPTURE, &pulsar_pcm_ops);
	snd_pcm_set_managed_buffer_all(pcm, SNDRV_DMA_TYPE_DEV, &chip->pci->dev,
				       PULSAR_MAX_CHANNELS * PULSAR_RING_BYTES,
				       PULSAR_MAX_CHANNELS * PULSAR_RING_BYTES);
	/* the card is already registered: register the new device (incl. /proc/asound entries) ... */
	err = snd_card_register(chip->card);
	if (err < 0)
		return err;
	chip->pcm = pcm;
	/* ... and tell udev/PipeWire to re-probe the card, which had no PCM when it first appeared */
	kobject_uevent(&chip->card->card_dev.kobj, KOBJ_CHANGE);
	return 0;
}

static bool slots_ok(const u16 *slots, unsigned int n, unsigned int lo, unsigned int hi)
{
	unsigned int i, j;

	if (n > PULSAR_MAX_CHANNELS)
		return false;
	for (i = 0; i < n; i++) {
		if (slots[i] < lo || slots[i] > hi)
			return false;
		for (j = 0; j < i; j++)
			if (slots[i] == slots[j])
				return false;
	}
	return true;
}

int pulsar_pcm_set_route(struct pulsar_card *chip, const struct pulsar_pcm_route *r)
{
	unsigned long flags;
	int err = 0;

	if (!r->rate || r->block < 64 || r->block > 1024 || (r->block & (r->block - 1)) ||
	    (!r->play_channels && !r->cap_channels) ||
	    !slots_ok(r->play_slot, r->play_channels, PULSAR_SLOT_PC_FIRST, PULSAR_SLOT_MAX) ||
	    !slots_ok(r->cap_slot, r->cap_channels, 0x40, PULSAR_SLOT_PC_FIRST - 1))
		return -EINVAL;

	mutex_lock(&chip->route_mutex);
	if (chip->playback.substream || chip->capture.substream) {
		err = -EBUSY;
		goto out;
	}
	spin_lock_irqsave(&chip->reg_lock, flags);
	chip->route = *r;
	table_layout(chip);
	chip->route_valid = true;
	spin_unlock_irqrestore(&chip->reg_lock, flags);

	/* both directions always exist; open() fails with -ENODEV for a direction the route lacks */
	if (!chip->pcm)
		err = pulsar_pcm_create(chip, true, true);
out:
	mutex_unlock(&chip->route_mutex);
	return err;
}
