// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Creamware / Sonic Core Pulsar 2: host-RAM delay lines ("PC delay atoms"), docs/pc_delay.md
 *
 * SCOPE runs every long delay (all delays, choruses, flangers, reverbs) through host memory and the
 * card's slot engine, without DSP code for the delay line itself (Sim2k atoms\pc_delay*.cpp, scScope.sys
 * slot types 0xE..0x17):
 *
 *   PULSAR_DELAY_4K / 32K  ("PC Master 4k/32k Delay"): the DSP output feeding "In" is captured into a
 *       0x1000 / 0x8000-word ring (entry P|1 / P|3). Each tap is a playback slot whose entry points into
 *       the SAME ring with a start offset, (0x82 - delay) << 2 (| 2 for the 32k ring): the card reads the
 *       ring `delay` samples behind the write position. No copying at all.
 *   PULSAR_DELAY_256K ("PC 256k Delay"): input ring 0x1000 (P|1); every block the IRQ copies the block just
 *       written into a 0x40000-word host line, and, for delays >= 2*block + 0x82, copies the delayed block
 *       into the output's own 0x1000-word ring (entry P). Shorter delays read the input ring like a 4k tap.
 *   PULSAR_DELAY_ER   ("PC Early Reflection"): input ring (P|1) into a 0x10000-word host line; the IRQ
 *       computes out = sum(gain_i * line[w - delay_i]) for up to 16 taps into the output's own ring.
 *
 * Delay values come either from the host (PULSAR_DELAY_P_DELAY) or from a DSP async output that the DSP
 * sends to a BAR SRAM dword (header 0x63E00000 | index); that dword is polled once per block.
 *
 * Slot table: the delay slots share the table with the PCM route (pulsar_pcm.c). Entries are written to
 * both banks; growing/shrinking the table moves the terminator in an order that never exposes a hole.
 */

#include <linux/pci.h>
#include <linux/io.h>
#include <linux/delay.h>
#include <linux/slab.h>
#include <linux/vmalloc.h>
#include <linux/dma-mapping.h>

#include "pulsar.h"

#define DLY_LAT            0x82        /* engine round trip DSP -> ring -> DSP, samples (scScope.sys) */
#define DLY_MIN            0xc2        /* minimum delay (DLY_LAT + one 64-word prefetch window) */
#define DLY_256K_LINE      0x40000     /* host line of "PC 256k Delay" (desc sub-buffer +0x10) */
#define DLY_256K_MAX       (DLY_256K_LINE - 0x400)
#define DLY_ER_LINE        0x10000     /* host line of "PC Early Reflection" */
#define SLOT_HDR_MASK      0xc03fffff

struct pulsar_delay_tap {
	u16 slot;                      /* playback slot (DM 0xC000 + 2*slot), 0 = none */
	s32 delay;                     /* samples, already clamped */
	u32 src_word;                  /* BAR SRAM dword index of a DSP async value, 0 = host value */
	s32 gain;                      /* ER: 1.31 */
	u32 entry;                     /* entry currently in the table */
};

struct pulsar_delay {
	struct list_head list;
	u32 handle;
	u32 kind;
	u16 write_slot;
	unsigned int ntaps;            /* playback slots in use */
	unsigned int er_taps;          /* ER: active reflections */
	struct pulsar_delay_tap tap[PULSAR_DELAY_MAX_TAPS];

	u32 ring_len;                  /* input ring, words (0x1000 or 0x8000) */
	u32 *ring;
	dma_addr_t ring_dma;
	u32 *out;                      /* 256K / ER: own playback ring, 0x1000 words */
	dma_addr_t out_dma;
	s32 *line;                     /* 256K / ER: host delay line */
	u32 line_len, line_w;
};

static void dslot_write(struct pulsar_card *chip, unsigned int s, u32 e)
{
	writel(e, chip->iobase + PULSAR_SLOT_A(s));
	writel(e, chip->iobase + PULSAR_SLOT_B(s));
}

static inline s32 mul31(s32 a, s32 b)        /* scScope.sys FUN_180001ac0: 1.31 x 1.31 */
{
	return (s32)(((s64)a * b) >> 31);
}

static u32 ring_flags(const struct pulsar_delay *d)
{
	return d->ring_len == 0x8000 ? 2 : 0;
}

static s32 clamp_delay(const struct pulsar_delay *d, s32 v)
{
	switch (d->kind) {
	case PULSAR_DELAY_4K:
	case PULSAR_DELAY_32K:
		return clamp_t(s32, v, DLY_MIN, d->ring_len);
	case PULSAR_DELAY_256K:
		return clamp_t(s32, v, DLY_MIN, DLY_256K_MAX);
	default:
		return v;              /* ER: handled per tap in er_offset() */
	}
}

/* two blocks of IRQ latency (copy of block k-1 into block k+1) + the engine round trip */
static u32 copy_threshold(struct pulsar_card *chip)
{
	return 2 * chip->route.block + DLY_LAT;
}

/* scScope.sys entry encoding FUN_180010440, cases 0x11/0x12/0x13 */
static u32 tap_entry(struct pulsar_card *chip, const struct pulsar_delay *d, unsigned int k)
{
	u32 off;

	if (d->kind == PULSAR_DELAY_ER)
		return lower_32_bits(d->out_dma);
	if (d->kind == PULSAR_DELAY_256K && d->tap[k].delay >= (s32)copy_threshold(chip))
		return lower_32_bits(d->out_dma);
	off = (DLY_LAT - d->tap[k].delay) & (d->ring_len - 1);
	return lower_32_bits(d->ring_dma) | off << 2 | ring_flags(d);
}

static u32 write_entry(const struct pulsar_delay *d)
{
	return lower_32_bits(d->ring_dma) | PULSAR_SLOT_CAPTURE | ring_flags(d);
}

static void delay_write_entries(struct pulsar_card *chip, struct pulsar_delay *d)
{
	unsigned int k;

	dslot_write(chip, d->write_slot, write_entry(d));
	for (k = 0; k < d->ntaps; k++) {
		d->tap[k].entry = tap_entry(chip, d, k);
		dslot_write(chip, d->tap[k].slot, d->tap[k].entry);
	}
}

static void delay_idle_entries(struct pulsar_card *chip, struct pulsar_delay *d)
{
	unsigned int k;

	for (k = 0; k < d->ntaps; k++)
		dslot_write(chip, d->tap[k].slot, PULSAR_SLOT_IDLE);
	dslot_write(chip, d->write_slot, PULSAR_SLOT_IDLE);
}

/* ---- slot table bookkeeping (caller holds reg_lock) ---------------------------------------------- */

unsigned int pulsar_delay_max_slot(struct pulsar_card *chip)
{
	struct pulsar_delay *d;
	unsigned int k, m = 0;

	list_for_each_entry(d, &chip->delays, list) {
		m = max_t(unsigned int, m, d->write_slot);
		for (k = 0; k < d->ntaps; k++)
			m = max_t(unsigned int, m, d->tap[k].slot);
	}
	return m;
}

/* called by table_layout() after it idled the table: put the delay lines back */
void pulsar_delay_restore_entries(struct pulsar_card *chip)
{
	struct pulsar_delay *d;

	list_for_each_entry(d, &chip->delays, list)
		delay_write_entries(chip, d);
}

static unsigned int route_max_slot(struct pulsar_card *chip)
{
	const struct pulsar_pcm_route *r = &chip->route;
	unsigned int c, m = PULSAR_SLOT_FIRST;

	for (c = 0; c < r->play_channels; c++)
		m = max_t(unsigned int, m, r->play_slot[c]);
	for (c = 0; c < r->cap_channels; c++)
		m = max_t(unsigned int, m, r->cap_slot[c]);
	return m;
}

/* Move the terminator to max(PCM, delays) + 1 and update the PC-slot count of header words 0/1
 * (rebuild @0x180010960). Growing: new entries first, then the new terminator, then the old one is
 * replaced by its entry. Shrinking: the new terminator first. */
static void table_extent(struct pulsar_card *chip)
{
	unsigned int maxslot = max(route_max_slot(chip), pulsar_delay_max_slot(chip));
	unsigned int end = maxslot + 1, old = chip->slot_end, s, n;
	u32 w0;

	if (end > old) {
		for (s = old + 1; s < end; s++)
			dslot_write(chip, s, PULSAR_SLOT_IDLE);
		pulsar_delay_restore_entries(chip);
		dslot_write(chip, end, 0);
		dslot_write(chip, old, PULSAR_SLOT_IDLE);
		pulsar_delay_restore_entries(chip);	/* in case `old` is one of ours */
	} else if (end < old) {
		dslot_write(chip, end, 0);
	}
	chip->slot_end = end;

	n = end > PULSAR_SLOT_PC_FIRST + 2 ? end - PULSAR_SLOT_PC_FIRST : 2;
	w0 = readl(chip->iobase + PULSAR_SLOT_A(0));
	w0 = (w0 & SLOT_HDR_MASK) | (n & 0x1f) << 25 | (n & 0xe0) << 17;
	writel(w0, chip->iobase + PULSAR_SLOT_A(0));
	writel(w0 + 1, chip->iobase + PULSAR_SLOT_A(1));
}

/* ---- slot ownership --------------------------------------------------------------------------------- */

static bool slot_used(struct pulsar_card *chip, unsigned int s)
{
	const struct pulsar_pcm_route *r = &chip->route;
	struct pulsar_delay *d;
	unsigned int c, k;

	for (c = 0; c < r->play_channels; c++)
		if (r->play_slot[c] == s)
			return true;
	for (c = 0; c < r->cap_channels; c++)
		if (r->cap_slot[c] == s)
			return true;
	list_for_each_entry(d, &chip->delays, list) {
		if (d->write_slot == s)
			return true;
		for (k = 0; k < d->ntaps; k++)
			if (d->tap[k].slot == s)
				return true;
	}
	return false;
}

static struct pulsar_delay *find_delay(struct pulsar_card *chip, u32 handle)
{
	struct pulsar_delay *d;

	list_for_each_entry(d, &chip->delays, list)
		if (d->handle == handle)
			return d;
	return NULL;
}

/* ---- memory -------------------------------------------------------------------------------------- */

static void delay_free_mem(struct pulsar_card *chip, struct pulsar_delay *d)
{
	struct device *dev = &chip->pci->dev;

	if (d->ring)
		dma_free_coherent(dev, d->ring_len * 4, d->ring, d->ring_dma);
	if (d->out)
		dma_free_coherent(dev, PULSAR_RING_BYTES, d->out, d->out_dma);
	vfree(d->line);
	kfree(d);
}

/* dma_alloc_coherent() aligns to the allocation order: 16 KB / 128 KB rings are naturally aligned,
 * as the entry format needs (low 14 / 17 bits carry flags and the start offset). */
static int ring_alloc(struct pulsar_card *chip, size_t bytes, u32 **va, dma_addr_t *dma)
{
	*va = dma_alloc_coherent(&chip->pci->dev, bytes, dma, GFP_KERNEL);
	if (!*va)
		return -ENOMEM;
	if ((*dma & (bytes - 1)) || upper_32_bits(*dma)) {
		dev_err(&chip->pci->dev, "delay ring at %pad not %zu-aligned below 4 GB\n", dma, bytes);
		return -ENOMEM;
	}
	return 0;
}

/* ---- ioctls ---------------------------------------------------------------------------------------- */

int pulsar_delay_alloc(struct pulsar_card *chip, struct pulsar_delay_alloc *a)
{
	struct pulsar_delay *d;
	unsigned long flags;
	unsigned int k, j, s, maxtaps;
	int err;

	switch (a->kind) {
	case PULSAR_DELAY_4K:
	case PULSAR_DELAY_32K:
		maxtaps = 8;
		break;
	case PULSAR_DELAY_256K:
	case PULSAR_DELAY_ER:
		maxtaps = 1;
		break;
	default:
		return -EINVAL;
	}
	if (!a->ntaps || a->ntaps > maxtaps ||
	    a->write_slot < 0x40 || a->write_slot >= PULSAR_SLOT_PC_FIRST)
		return -EINVAL;
	for (k = 0; k < a->ntaps; k++) {
		if (a->tap_slot[k] && (a->tap_slot[k] < PULSAR_SLOT_PC_FIRST || a->tap_slot[k] > PULSAR_SLOT_MAX))
			return -EINVAL;
		for (j = 0; j < k; j++)
			if (a->tap_slot[k] && a->tap_slot[k] == a->tap_slot[j])
				return -EINVAL;
	}

	d = kzalloc(sizeof(*d), GFP_KERNEL);
	if (!d)
		return -ENOMEM;
	d->kind = a->kind;
	d->write_slot = a->write_slot;
	d->ntaps = a->ntaps;
	d->ring_len = a->kind == PULSAR_DELAY_32K ? 0x8000 : PULSAR_RING_FRAMES;
	err = ring_alloc(chip, d->ring_len * 4, &d->ring, &d->ring_dma);
	if (err)
		goto fail;
	if (a->kind == PULSAR_DELAY_256K || a->kind == PULSAR_DELAY_ER) {
		err = ring_alloc(chip, PULSAR_RING_BYTES, &d->out, &d->out_dma);
		if (err)
			goto fail;
		d->line_len = a->kind == PULSAR_DELAY_256K ? DLY_256K_LINE : DLY_ER_LINE;
		d->line = vzalloc(d->line_len * sizeof(s32));
		if (!d->line) {
			err = -ENOMEM;
			goto fail;
		}
	}
	if (a->kind == PULSAR_DELAY_ER) {
		d->er_taps = PULSAR_DELAY_MAX_TAPS;
		for (k = 0; k < PULSAR_DELAY_MAX_TAPS; k++)
			d->tap[k].delay = a->delay[k];
	} else {
		for (k = 0; k < d->ntaps; k++)
			d->tap[k].delay = clamp_delay(d, a->delay[k]);
	}

	mutex_lock(&chip->delay_mutex);
	spin_lock_irqsave(&chip->reg_lock, flags);
	err = -ENODEV;
	if (!chip->route_valid)                 /* need the block size and the table layout */
		goto unlock;
	err = -EBUSY;
	if (slot_used(chip, d->write_slot))
		goto unlock;
	for (k = 0; k < d->ntaps; k++) {
		if (a->tap_slot[k]) {
			if (slot_used(chip, a->tap_slot[k]))
				goto unlock;
			d->tap[k].slot = a->tap_slot[k];
		}
	}
	for (k = 0; k < d->ntaps; k++) {        /* auto-pick: lowest free PC slot */
		if (d->tap[k].slot)
			continue;
		for (s = PULSAR_SLOT_PC_FIRST; s <= PULSAR_SLOT_MAX; s++) {
			for (j = 0; j < d->ntaps; j++)
				if (d->tap[j].slot == s)
					break;
			if (j == d->ntaps && !slot_used(chip, s))
				break;
		}
		if (s > PULSAR_SLOT_MAX)
			goto unlock;
		d->tap[k].slot = s;
	}
	d->handle = ++chip->delay_next_handle;
	/* start of a write slot (FUN_1800146e0): clear its window and the windows of its taps */
	memset_io(chip->iobase + PULSAR_SLOT_WIN(d->write_slot), 0, 0x200);
	for (k = 0; k < d->ntaps; k++)
		memset_io(chip->iobase + PULSAR_SLOT_WIN(d->tap[k].slot), 0, 0x200);
	list_add_tail(&d->list, &chip->delays);
	delay_write_entries(chip, d);
	table_extent(chip);
	err = 0;
unlock:
	spin_unlock_irqrestore(&chip->reg_lock, flags);
	mutex_unlock(&chip->delay_mutex);
	if (err)
		goto fail;

	a->handle = d->handle;
	for (k = 0; k < d->ntaps; k++)
		a->tap_slot[k] = d->tap[k].slot;
	return 0;
fail:
	delay_free_mem(chip, d);
	return err;
}

/* caller holds delay_mutex */
static void delay_release(struct pulsar_card *chip, struct pulsar_delay *d)
{
	unsigned long flags;

	spin_lock_irqsave(&chip->reg_lock, flags);
	delay_idle_entries(chip, d);
	list_del(&d->list);
	table_extent(chip);
	spin_unlock_irqrestore(&chip->reg_lock, flags);
	/* the engine may still prefetch from the rings: wait two blocks before freeing them */
	synchronize_irq(chip->irq);
	msleep(chip->route.rate ? 2 * 1000 * chip->route.block / chip->route.rate + 10 : 60);
	delay_free_mem(chip, d);
}

int pulsar_delay_free(struct pulsar_card *chip, u32 handle)
{
	struct pulsar_delay *d;

	mutex_lock(&chip->delay_mutex);
	d = find_delay(chip, handle);
	if (d)
		delay_release(chip, d);
	mutex_unlock(&chip->delay_mutex);
	return d ? 0 : -ENOENT;
}

void pulsar_delay_free_all(struct pulsar_card *chip)
{
	struct pulsar_delay *d;

	mutex_lock(&chip->delay_mutex);
	while (!list_empty(&chip->delays)) {
		d = list_first_entry(&chip->delays, struct pulsar_delay, list);
		delay_release(chip, d);
	}
	mutex_unlock(&chip->delay_mutex);
}

/* caller holds reg_lock */
static void tap_update(struct pulsar_card *chip, struct pulsar_delay *d, unsigned int k)
{
	u32 e;

	if (d->kind == PULSAR_DELAY_ER || k >= d->ntaps)
		return;
	e = tap_entry(chip, d, k);
	if (e != d->tap[k].entry) {
		d->tap[k].entry = e;
		dslot_write(chip, d->tap[k].slot, e);
	}
}

int pulsar_delay_param(struct pulsar_card *chip, const struct pulsar_delay_param *p)
{
	struct pulsar_delay *d;
	unsigned long flags;
	unsigned int ntap;
	int err = 0;

	mutex_lock(&chip->delay_mutex);
	spin_lock_irqsave(&chip->reg_lock, flags);
	d = find_delay(chip, p->handle);
	if (!d) {
		err = -ENOENT;
		goto out;
	}
	ntap = d->kind == PULSAR_DELAY_ER ? PULSAR_DELAY_MAX_TAPS : d->ntaps;
	if (p->index >= ntap && p->param != PULSAR_DELAY_P_NTAPS) {
		err = -EINVAL;
		goto out;
	}
	switch (p->param) {
	case PULSAR_DELAY_P_DELAY:
		d->tap[p->index].delay = d->kind == PULSAR_DELAY_ER ? p->value : clamp_delay(d, p->value);
		tap_update(chip, d, p->index);
		break;
	case PULSAR_DELAY_P_SOURCE:
		/* async DSP->PC dwords live in the windows of the reserved slots 0x10..0x3f */
		if (d->kind == PULSAR_DELAY_ER || (p->value && (p->value < 0x800 || p->value > 0x1fff))) {
			err = -EINVAL;
			break;
		}
		d->tap[p->index].src_word = p->value;
		if (p->value)
			writel(0, chip->iobase + 0x80000 + 4 * p->value);	/* 0 = no value yet */
		break;
	case PULSAR_DELAY_P_GAIN:
		if (d->kind != PULSAR_DELAY_ER)
			err = -EINVAL;
		else
			d->tap[p->index].gain = p->value;
		break;
	case PULSAR_DELAY_P_NTAPS:
		if (d->kind != PULSAR_DELAY_ER)
			err = -EINVAL;
		else
			d->er_taps = clamp_t(s32, p->value, 0, PULSAR_DELAY_MAX_TAPS);
		break;
	default:
		err = -EINVAL;
	}
out:
	spin_unlock_irqrestore(&chip->reg_lock, flags);
	mutex_unlock(&chip->delay_mutex);
	return err;
}

/* ---- per-block service (scScope.sys slot_service @0x180011bd0 / copy @0x180010c60) ---------------- */

static void copy_from_ring(const u32 *ring, u32 mask, u32 pos, s32 *dst, u32 dst_len, u32 *w, u32 n)
{
	u32 i;

	for (i = 0; i < n; i++) {
		dst[*w] = (s32)READ_ONCE(ring[(pos + i) & mask]);
		if (++*w >= dst_len)
			*w = 0;
	}
}

static void service_256k(struct pulsar_card *chip, struct pulsar_delay *d, u32 prev, u32 next, u32 blk)
{
	u32 thr = copy_threshold(chip), w0 = d->line_w, r, i;
	s32 dl = d->tap[0].delay;

	copy_from_ring(d->ring, d->ring_len - 1, prev, d->line, d->line_len, &d->line_w, blk);
	if (dl < (s32)thr)
		return;                             /* the tap reads the input ring directly */
	r = (w0 + d->line_len - (u32)(dl - thr) % d->line_len) % d->line_len;
	for (i = 0; i < blk; i++) {
		WRITE_ONCE(d->out[(next + i) & (PULSAR_RING_FRAMES - 1)], (u32)d->line[r]);
		if (++r >= d->line_len)
			r = 0;
	}
}

/* FUN_180007a30. Taps shorter than the copy path are muted, as Sim2k does (gain 0 below 0x882). */
static void service_er(struct pulsar_card *chip, struct pulsar_delay *d, u32 prev, u32 next, u32 blk)
{
	u32 thr = copy_threshold(chip), off[PULSAR_DELAY_MAX_TAPS], i, t, n = d->er_taps;
	s32 g[PULSAR_DELAY_MAX_TAPS];

	for (t = 0; t < n; t++) {
		s32 v = d->tap[t].delay;

		g[t] = v < (s32)thr ? 0 : d->tap[t].gain;
		off[t] = v < (s32)thr ? 0 : min_t(u32, v - thr, d->line_len - 1);
	}
	for (i = 0; i < blk; i++) {
		s64 acc = 0;

		d->line[d->line_w] = (s32)READ_ONCE(d->ring[(prev + i) & (d->ring_len - 1)]);
		for (t = 0; t < n; t++)
			acc += mul31(d->line[(d->line_w + d->line_len - off[t]) % d->line_len], g[t]);
		/* Windows adds with 32-bit wrap-around; saturate instead */
		WRITE_ONCE(d->out[(next + i) & (PULSAR_RING_FRAMES - 1)], (u32)(s32)clamp_t(s64, acc, S32_MIN, S32_MAX));
		if (++d->line_w >= d->line_len)
			d->line_w = 0;
	}
}

/* Called from the ISR after the IRQ was acknowledged (one IRQ per route.block frames). */
void pulsar_delay_interrupt(struct pulsar_card *chip)
{
	struct pulsar_delay *d;
	u32 cur, prev, next, blk, k;
	s32 v;

	spin_lock(&chip->reg_lock);
	if (list_empty(&chip->delays) || !chip->route_valid)
		goto out;
	blk = chip->route.block;
	cur = readl(chip->iobase + PULSAR_REG_SAMPLE_COUNT);
	prev = (cur - blk) & ~(blk - 1) & 0x7fff;
	next = (cur + blk) & ~(blk - 1) & 0x7fff;
	if (prev == chip->delay_last_block)
		goto out;
	chip->delay_last_block = prev;

	list_for_each_entry(d, &chip->delays, list) {
		for (k = 0; k < d->ntaps; k++) {    /* delay time sent by a DSP module (e.g. DLEXTM1 "DT") */
			if (!d->tap[k].src_word)
				continue;
			v = (s32)readl(chip->iobase + 0x80000 + 4 * d->tap[k].src_word);
			if (v <= 0)
				continue;
			v = clamp_delay(d, v);
			if (v != d->tap[k].delay) {
				d->tap[k].delay = v;
				tap_update(chip, d, k);
			}
		}
		if (d->kind == PULSAR_DELAY_256K) {
			service_256k(chip, d, prev, next, blk);
			tap_update(chip, d, 0);     /* switches between direct and copied path */
		} else if (d->kind == PULSAR_DELAY_ER) {
			service_er(chip, d, prev, next, blk);
		}
	}
out:
	spin_unlock(&chip->reg_lock);
}

void pulsar_delay_init(struct pulsar_card *chip)
{
	INIT_LIST_HEAD(&chip->delays);
	mutex_init(&chip->delay_mutex);
	chip->slot_end = PULSAR_SLOT_FIRST;
	chip->delay_last_block = U32_MAX;
}
