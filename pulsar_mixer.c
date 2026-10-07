// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Creamware / Sonic Core Pulsar 2 mixer controls. Each control is a gain value of a DSP module
 * (LINVOL "Vol" sync input, 1.31 fraction, 2-word slot) described by the loader with
 * PULSAR_IOCTL_SET_CONTROLS. Range -60..0 dB in 0.5 dB steps, 0 = mute.
 */

#include <sound/core.h>
#include <sound/control.h>
#include <sound/tlv.h>

#include "pulsar.h"

#define GAIN_STEPS	120			/* 0.5 dB steps, -60..0 dB */

/* round(10^((-60 + 0.5*i)/20) * 0x7fffffff), i = 0 is mute */
static const u32 gain_table[GAIN_STEPS + 1] = {
	0x00000000, 0x0022b5aa, 0x0024c42c, 0x0026f1e1, 0x002940a2, 0x002bb263,
	0x002e4939, 0x00310756, 0x0033ef0c, 0x003702d4, 0x003a454a, 0x003db932,
	0x00416179, 0x0045413b, 0x00495bc1, 0x004db486, 0x00524f3b, 0x00572fc8,
	0x005c5a4f, 0x0061d334, 0x00679f1c, 0x006dc2f0, 0x007443e8, 0x007b2787,
	0x008273a6, 0x008a2e77, 0x00925e89, 0x009b0ace, 0x00a43aa2, 0x00adf5d1,
	0x00b8449c, 0x00c32fc3, 0x00cec08a, 0x00db00c0, 0x00e7facc, 0x00f5b9b0,
	0x01044915, 0x0113b557, 0x01240b8c, 0x01355991, 0x0147ae14, 0x015b18a5,
	0x016fa9bb, 0x018572cb, 0x019c8651, 0x01b4f7e3, 0x01cedc3d, 0x01ea4958,
	0x0207567a, 0x02261c4a, 0x0246b4e4, 0x02693bf0, 0x028dcebc, 0x02b48c50,
	0x02dd958a, 0x03090d3f, 0x0337184e, 0x0367ddcc, 0x039b8719, 0x03d2400c,
	0x040c3714, 0x04499d60, 0x048aa70b, 0x04cf8b44, 0x05188480, 0x0565d0ab,
	0x05b7b15b, 0x060e6c0b, 0x066a4a53, 0x06cb9a26, 0x0732ae18, 0x079fdd9f,
	0x08138562, 0x088e0783, 0x090fcbf7, 0x099940db, 0x0a2adad1, 0x0ac51567,
	0x0b68737a, 0x0c157fa9, 0x0ccccccd, 0x0d8ef66d, 0x0e5ca14c, 0x0f367bee,
	0x101d3f2d, 0x1111aedb, 0x12149a60, 0x1326dd70, 0x144960c5, 0x157d1ae2,
	0x16c310e3, 0x181c5762, 0x198a1357, 0x1b0d7b1b, 0x1ca7d768, 0x1e5a8471,
	0x2026f30f, 0x220ea9f4, 0x241346f6, 0x26368073, 0x287a26c4, 0x2ae025c3,
	0x2d6a866f, 0x301b70a8, 0x32f52cff, 0x35fa26a9, 0x392ced8e, 0x3c90386f,
	0x4026e73c, 0x43f4057e, 0x47faccf0, 0x4c3ea838, 0x50c335d3, 0x558c4b22,
	0x5a9df7ab, 0x5ffc8890, 0x65ac8c2e, 0x6bb2d603, 0x721482bf, 0x78d6fc9e,
	0x7fffffff,
};

static const DECLARE_TLV_DB_SCALE(pulsar_db_scale, -6000, 50, 1);

static int ctl_info(struct snd_kcontrol *kc, struct snd_ctl_elem_info *ui)
{
	struct pulsar_card *chip = snd_kcontrol_chip(kc);

	ui->type = SNDRV_CTL_ELEM_TYPE_INTEGER;
	ui->count = chip->controls.ctl[kc->private_value].channels;
	ui->value.integer.min = 0;
	ui->value.integer.max = GAIN_STEPS;
	return 0;
}

static int ctl_get(struct snd_kcontrol *kc, struct snd_ctl_elem_value *uv)
{
	struct pulsar_card *chip = snd_kcontrol_chip(kc);
	unsigned int i = kc->private_value, c;

	mutex_lock(&chip->route_mutex);
	for (c = 0; c < chip->controls.ctl[i].channels; c++)
		uv->value.integer.value[c] = chip->ctl_val[i][c];
	mutex_unlock(&chip->route_mutex);
	return 0;
}

static int ctl_write(struct pulsar_card *chip, unsigned int i, unsigned int c)
{
	const struct pulsar_ctl_desc *d = &chip->controls.ctl[i];
	u32 g = gain_table[chip->ctl_val[i][c]];
	int err;

	/* a sync value slot is two words (double-buffered); write both */
	err = pulsar_dsp_set_value(chip, d->dsp, d->addr[c], g);
	if (!err)
		err = pulsar_dsp_set_value(chip, d->dsp, d->addr[c] + 1, g);
	return err;
}

static int ctl_put(struct snd_kcontrol *kc, struct snd_ctl_elem_value *uv)
{
	struct pulsar_card *chip = snd_kcontrol_chip(kc);
	unsigned int i = kc->private_value, c;
	int changed = 0, err = 0;

	mutex_lock(&chip->route_mutex);
	for (c = 0; c < chip->controls.ctl[i].channels; c++) {
		long v = uv->value.integer.value[c];

		if (v < 0 || v > GAIN_STEPS) {
			err = -EINVAL;
			break;
		}
		if (v == chip->ctl_val[i][c])
			continue;
		chip->ctl_val[i][c] = v;
		err = ctl_write(chip, i, c);
		if (err)
			break;
		changed = 1;
	}
	mutex_unlock(&chip->route_mutex);
	return err ? err : changed;
}

static unsigned int cdb_to_step(s32 cdb)
{
	cdb = clamp(cdb, -6000, 0);
	return (cdb + 6000 + 25) / 50;
}

int pulsar_mixer_set_controls(struct pulsar_card *chip, const struct pulsar_controls *pc)
{
	unsigned int i, c;
	int err = 0;

	if (pc->count > PULSAR_MAX_CONTROLS)
		return -EINVAL;
	for (i = 0; i < pc->count; i++) {
		const struct pulsar_ctl_desc *d = &pc->ctl[i];

		if (d->dsp > 5 || d->channels < 1 || d->channels > 2 || !memchr(d->name, 0, sizeof(d->name)))
			return -EINVAL;
		for (c = 0; c < d->channels; c++)
			if (d->addr[c] < 0xc400 || d->addr[c] > 0xdffe)
				return -EINVAL;
	}

	mutex_lock(&chip->route_mutex);
	/* the set of controls is fixed once created; later calls only re-point them (new DSP graph) */
	if (chip->ctl_count && (pc->count != chip->ctl_count ||
				memcmp(pc->ctl[0].name, chip->controls.ctl[0].name, sizeof(pc->ctl[0].name)))) {
		err = -EBUSY;
		goto out;
	}
	chip->controls = *pc;
	for (i = 0; i < pc->count; i++)
		for (c = 0; c < pc->ctl[i].channels; c++) {
			chip->ctl_val[i][c] = cdb_to_step(pc->ctl[i].init_cdb);
			err = ctl_write(chip, i, c);
			if (err)
				goto out;
		}
	if (!chip->ctl_count) {
		for (i = 0; i < pc->count; i++) {
			struct snd_kcontrol_new kn = {
				.iface = SNDRV_CTL_ELEM_IFACE_MIXER,
				.name = chip->controls.ctl[i].name,
				.access = SNDRV_CTL_ELEM_ACCESS_READWRITE | SNDRV_CTL_ELEM_ACCESS_TLV_READ,
				.info = ctl_info,
				.get = ctl_get,
				.put = ctl_put,
				.tlv = { .p = pulsar_db_scale },
				.private_value = i,
			};

			err = snd_ctl_add(chip->card, snd_ctl_new1(&kn, chip));
			if (err < 0)
				goto out;
		}
		chip->ctl_count = pc->count;
	}
out:
	mutex_unlock(&chip->route_mutex);
	return err;
}
