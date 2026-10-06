// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Creamware / Sonic Core Pulsar 2 & Scope ALSA Core Driver
 *
 * Reverse-engineered hardware implementation for Linux
 */

#include <linux/module.h>
#include <linux/init.h>
#include <linux/pci.h>
#include <linux/interrupt.h>
#include <linux/io.h>
#include <sound/core.h>
#include <sound/initval.h>

#include "pulsar.h"

MODULE_AUTHOR("Sonic Core / Creamware Linux Open-Source Project");
MODULE_DESCRIPTION(DRV_DESC);
MODULE_LICENSE("GPL");

static int index[SNDRV_CARDS] = SNDRV_DEFAULT_IDX;
static char *id[SNDRV_CARDS] = SNDRV_DEFAULT_STR;
static bool enable[SNDRV_CARDS] = SNDRV_DEFAULT_ENABLE_PNP;

module_param_array(index, int, NULL, 0444);
MODULE_PARM_DESC(index, "Index value for Creamware Pulsar/Scope soundcard.");
module_param_array(id, charp, NULL, 0444);
MODULE_PARM_DESC(id, "ID string for Creamware Pulsar/Scope soundcard.");
module_param_array(enable, bool, NULL, 0444);
MODULE_PARM_DESC(enable, "Enable Creamware Pulsar/Scope soundcard.");

static const struct pci_device_id snd_pulsar_ids[] = {
	{ PCI_DEVICE(PCI_VENDOR_ID_CREAMWARE, PCI_DEVICE_ID_SCOPE_SP) },
	{ PCI_DEVICE(PCI_VENDOR_ID_CREAMWARE, PCI_DEVICE_ID_PULSAR1) },
	{ PCI_DEVICE(PCI_VENDOR_ID_CREAMWARE, PCI_DEVICE_ID_PULSAR_SRB) },
	{ PCI_DEVICE(PCI_VENDOR_ID_CREAMWARE, PCI_DEVICE_ID_PULSAR2) },
	{ PCI_DEVICE(PCI_VENDOR_ID_CREAMWARE, PCI_DEVICE_ID_SCOPE_PRO_8) },
	{ PCI_DEVICE(PCI_VENDOR_ID_CREAMWARE, PCI_DEVICE_ID_SCOPE_PRO_9) },
	{ PCI_DEVICE(PCI_VENDOR_ID_CREAMWARE, PCI_DEVICE_ID_SCOPE_PRO_A) },
	{ PCI_DEVICE(PCI_VENDOR_ID_CREAMWARE, PCI_DEVICE_ID_SCOPE_PRO_B) },
	{ 0, }
};
MODULE_DEVICE_TABLE(pci, snd_pulsar_ids);

static irqreturn_t snd_pulsar_interrupt(int irq, void *dev_id)
{
	struct pulsar_card *chip = dev_id;
	u32 status;

	if (!chip || !chip->iobase)
		return IRQ_NONE;

	spin_lock(&chip->reg_lock);
	status = readl(chip->iobase + PULSAR_REG_INT_STATUS);
	if (!(status & PULSAR_REG_INT_STATUS_MASK) && !(status & 0xffff0000)) {
		spin_unlock(&chip->reg_lock);
		return IRQ_NONE;
	}

	/* Acknowledge interrupt (clear bit by writing 0 to 0x1c) */
	writel(0x00, chip->iobase + PULSAR_REG_INT_ACK);
	spin_unlock(&chip->reg_lock);

	/* Notify PCM subsystem that a period elapsed */
	pulsar_pcm_period_elapsed(chip);

	return IRQ_HANDLED;
}

static int snd_pulsar_free(struct pulsar_card *chip)
{
	if (chip->irq >= 0)
		free_irq(chip->irq, chip);

	if (chip->iobase)
		pci_iounmap(chip->pci, chip->iobase);

	if (chip->iobase_phys)
		pci_release_regions(chip->pci);

	pci_disable_device(chip->pci);
	return 0;
}

static int snd_pulsar_dev_free(struct snd_device *device)
{
	struct pulsar_card *chip = device->device_data;
	return snd_pulsar_free(chip);
}

static int snd_pulsar_probe(struct pci_dev *pci, const struct pci_device_id *pci_id)
{
	static int dev;
	struct snd_card *card;
	struct pulsar_card *chip;
	static const struct snd_device_ops ops = {
		.dev_free = snd_pulsar_dev_free,
	};
	int err;

	if (dev >= SNDRV_CARDS)
		return -ENODEV;
	if (!enable[dev]) {
		dev++;
		return -ENOENT;
	}

	err = snd_card_new(&pci->dev, index[dev], id[dev], THIS_MODULE,
			   sizeof(struct pulsar_card), &card);
	if (err < 0)
		return err;

	chip = card->private_data;
	chip->card = card;
	chip->pci = pci;
	chip->irq = -1;
	spin_lock_init(&chip->reg_lock);
	mutex_init(&chip->ioctl_mutex);

	err = pci_enable_device(pci);
	if (err < 0) {
		dev_err(&pci->dev, "pci_enable_device failed: %d\n", err);
		goto error_free_card;
	}

	pci_set_master(pci);

	err = pci_request_regions(pci, DRV_NAME);
	if (err < 0) {
		dev_err(&pci->dev, "pci_request_regions failed: %d\n", err);
		goto error_disable_pci;
	}

	chip->iobase_phys = pci_resource_start(pci, 0);
	chip->iobase_len = pci_resource_len(pci, 0);

	chip->iobase = pci_iomap(pci, 0, 0);
	if (!chip->iobase) {
		dev_err(&pci->dev, "Unable to iomap BAR 0 (4MB MMIO window)\n");
		err = -ENOMEM;
		goto error_release_regions;
	}

	/* Read Hardware Status & Board Revision (Offset 0x00) */
	chip->board_raw_id = readl(chip->iobase + PULSAR_REG_BOARD_ID);
	chip->board_rev = (chip->board_raw_id >> PULSAR_REG_BOARD_REV_SHIFT) & PULSAR_REG_BOARD_REV_MASK;

	dev_info(&pci->dev, "Creamware/SonicCore card detected at MMIO 0x%lx (len: %lu KB)\n",
		 chip->iobase_phys, chip->iobase_len / 1024);
	dev_info(&pci->dev, "Hardware ID: 0x%08x, Detected Board Revision: %u\n",
		 chip->board_raw_id, chip->board_rev);

	/* Set 32-bit DMA Mask */
	err = dma_set_mask_and_coherent(&pci->dev, DMA_BIT_MASK(32));
	if (err) {
		dev_warn(&pci->dev, "Could not set 32-bit DMA mask: %d\n", err);
	}

	/* Request IRQ */
	err = request_irq(pci->irq, snd_pulsar_interrupt, IRQF_SHARED,
			  KBUILD_MODNAME, chip);
	if (err < 0) {
		dev_err(&pci->dev, "Cannot grab IRQ %d: %d\n", pci->irq, err);
		goto error_unmap_io;
	}
	chip->irq = pci->irq;

	err = snd_device_new(card, SNDRV_DEV_LOWLEVEL, chip, &ops);
	if (err < 0)
		goto error_free_irq;

	/* Setup ALSA PCM */
	err = pulsar_pcm_create(chip);
	if (err < 0) {
		dev_err(&pci->dev, "Failed to create ALSA PCM device: %d\n", err);
		goto error_free_irq;
	}

	/* Card Name */
	strcpy(card->driver, "Pulsar2");
	strcpy(card->shortname, "SonicCore Pulsar2");
	snprintf(card->longname, sizeof(card->longname),
		 "%s at 0x%lx, irq %d (rev %d)",
		 card->shortname, chip->iobase_phys, chip->irq, chip->board_rev);

	err = snd_card_register(card);
	if (err < 0) {
		dev_err(&pci->dev, "snd_card_register failed: %d\n", err);
		goto error_free_irq;
	}

	pci_set_drvdata(pci, card);
	dev++;
	return 0;

error_free_irq:
	free_irq(chip->irq, chip);
	chip->irq = -1;
error_unmap_io:
	pci_iounmap(pci, chip->iobase);
error_release_regions:
	pci_release_regions(pci);
error_disable_pci:
	pci_disable_device(pci);
error_free_card:
	snd_card_free(card);
	return err;
}

static void snd_pulsar_remove(struct pci_dev *pci)
{
	struct snd_card *card = pci_get_drvdata(pci);

	if (card)
		snd_card_free(card);
}

static struct pci_driver snd_pulsar_driver = {
	.name = DRV_NAME,
	.id_table = snd_pulsar_ids,
	.probe = snd_pulsar_probe,
	.remove = snd_pulsar_remove,
};

module_pci_driver(snd_pulsar_driver);
