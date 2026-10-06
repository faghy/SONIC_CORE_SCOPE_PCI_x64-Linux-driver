// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Creamware / Sonic Core Pulsar 2 & Scope ALSA PCM Interface
 */

#include <linux/pci.h>
#include <sound/core.h>
#include <sound/pcm.h>
#include <sound/pcm_params.h>

#include "pulsar.h"

static const struct snd_pcm_hardware snd_pulsar_pcm_hw = {
	.info = (SNDRV_PCM_INFO_MMAP |
		 SNDRV_PCM_INFO_INTERLEAVED |
		 SNDRV_PCM_INFO_BLOCK_TRANSFER |
		 SNDRV_PCM_INFO_MMAP_VALID |
		 SNDRV_PCM_INFO_PAUSE |
		 SNDRV_PCM_INFO_RESUME),
	.formats = (SNDRV_PCM_FMTBIT_S16_LE |
		    SNDRV_PCM_FMTBIT_S24_3LE |
		    SNDRV_PCM_FMTBIT_S32_LE),
	.rates = (SNDRV_PCM_RATE_44100 |
		  SNDRV_PCM_RATE_48000 |
		  SNDRV_PCM_RATE_88200 |
		  SNDRV_PCM_RATE_96000),
	.rate_min = 44100,
	.rate_max = 96000,
	.channels_min = 2,
	.channels_max = 16,
	.buffer_bytes_max = 256 * 1024,
	.period_bytes_min = 256,
	.period_bytes_max = 64 * 1024,
	.periods_min = 2,
	.periods_max = 32,
};

static int snd_pulsar_playback_open(struct snd_pcm_substream *substream)
{
	struct pulsar_card *chip = snd_pcm_substream_chip(substream);
	struct snd_pcm_runtime *runtime = substream->runtime;

	chip->playback_substream = substream;
	atomic_set(&chip->current_period, 0);
	runtime->hw = snd_pulsar_pcm_hw;
	return 0;
}

static int snd_pulsar_playback_close(struct snd_pcm_substream *substream)
{
	struct pulsar_card *chip = snd_pcm_substream_chip(substream);

	chip->playback_substream = NULL;
	return 0;
}

static int snd_pulsar_capture_open(struct snd_pcm_substream *substream)
{
	struct pulsar_card *chip = snd_pcm_substream_chip(substream);
	struct snd_pcm_runtime *runtime = substream->runtime;

	chip->capture_substream = substream;
	runtime->hw = snd_pulsar_pcm_hw;
	return 0;
}

static int snd_pulsar_capture_close(struct snd_pcm_substream *substream)
{
	struct pulsar_card *chip = snd_pcm_substream_chip(substream);

	chip->capture_substream = NULL;
	return 0;
}

static int snd_pulsar_hw_params(struct snd_pcm_substream *substream,
				struct snd_pcm_hw_params *hw_params)
{
	return 0;
}

static int snd_pulsar_hw_free(struct snd_pcm_substream *substream)
{
	return 0;
}

static int snd_pulsar_prepare(struct snd_pcm_substream *substream)
{
	struct pulsar_card *chip = snd_pcm_substream_chip(substream);

	atomic_set(&chip->current_period, 0);
	return 0;
}

static int snd_pulsar_trigger(struct snd_pcm_substream *substream, int cmd)
{
	switch (cmd) {
	case SNDRV_PCM_TRIGGER_START:
	case SNDRV_PCM_TRIGGER_RESUME:
		/* Start DMA streaming */
		return 0;
	case SNDRV_PCM_TRIGGER_STOP:
	case SNDRV_PCM_TRIGGER_SUSPEND:
		/* Stop DMA streaming */
		return 0;
	default:
		return -EINVAL;
	}
}

static snd_pcm_uframes_t snd_pulsar_pointer(struct snd_pcm_substream *substream)
{
	struct pulsar_card *chip = snd_pcm_substream_chip(substream);
	struct snd_pcm_runtime *runtime = substream->runtime;
	unsigned int period = atomic_read(&chip->current_period);
	snd_pcm_uframes_t pos;

	if (!runtime || runtime->period_size == 0)
		return 0;

	pos = (period * runtime->period_size) % runtime->buffer_size;
	return pos;
}

void pulsar_pcm_period_elapsed(struct pulsar_card *chip)
{
	if (chip->playback_substream) {
		atomic_inc(&chip->current_period);
		snd_pcm_period_elapsed(chip->playback_substream);
	}
	if (chip->capture_substream) {
		snd_pcm_period_elapsed(chip->capture_substream);
	}
}

static const struct snd_pcm_ops snd_pulsar_playback_ops = {
	.open = snd_pulsar_playback_open,
	.close = snd_pulsar_playback_close,
	.hw_params = snd_pulsar_hw_params,
	.hw_free = snd_pulsar_hw_free,
	.prepare = snd_pulsar_prepare,
	.trigger = snd_pulsar_trigger,
	.pointer = snd_pulsar_pointer,
};

static const struct snd_pcm_ops snd_pulsar_capture_ops = {
	.open = snd_pulsar_capture_open,
	.close = snd_pulsar_capture_close,
	.hw_params = snd_pulsar_hw_params,
	.hw_free = snd_pulsar_hw_free,
	.prepare = snd_pulsar_prepare,
	.trigger = snd_pulsar_trigger,
	.pointer = snd_pulsar_pointer,
};

int pulsar_pcm_create(struct pulsar_card *chip)
{
	struct snd_pcm *pcm;
	int err;

	err = snd_pcm_new(chip->card, "Pulsar PCM", 0, 1, 1, &pcm);
	if (err < 0)
		return err;

	pcm->private_data = chip;
	strcpy(pcm->name, "Pulsar2 DSP Audio");
	chip->pcm = pcm;

	snd_pcm_set_ops(pcm, SNDRV_PCM_STREAM_PLAYBACK, &snd_pulsar_playback_ops);
	snd_pcm_set_ops(pcm, SNDRV_PCM_STREAM_CAPTURE, &snd_pulsar_capture_ops);

	/* Pre-allocate managed DMA buffers for modern Linux kernels */
	snd_pcm_set_managed_buffer_all(pcm, SNDRV_DMA_TYPE_DEV,
				       &chip->pci->dev,
				       64 * 1024, 256 * 1024);

	return 0;
}
