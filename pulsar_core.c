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

/* Mirrors scScope.sys ISR @ 0x180007090: ack first, then read status. */
static irqreturn_t snd_pulsar_interrupt(int irq, void *dev_id)
{
	struct pulsar_card *chip = dev_id;
	u32 status;

	spin_lock(&chip->reg_lock);
	readl(chip->iobase + PULSAR_REG_SAMPLE_COUNT);
	writel(0, chip->iobase + PULSAR_REG_INT_ACK);
	status = readl(chip->iobase + PULSAR_REG_INT_STATUS);
	spin_unlock(&chip->reg_lock);

	if (!(status & PULSAR_INT_PENDING_MASK) ||
	    (status & PULSAR_INT_INVALID_MASK))
		return IRQ_NONE;

	WRITE_ONCE(chip->last_int_status, status);
	atomic_inc(&chip->irq_count);

	if (chip->pcm)
		pulsar_pcm_interrupt(chip);

	return IRQ_HANDLED;
}

static int dev;

static int __snd_pulsar_probe(struct pci_dev *pci, const struct pci_device_id *pci_id)
{
	struct snd_card *card;
	struct pulsar_card *chip;
	int err;

	if (dev >= SNDRV_CARDS)
		return -ENODEV;
	if (!enable[dev]) {
		dev++;
		return -ENOENT;
	}

	err = snd_devm_card_new(&pci->dev, index[dev], id[dev], THIS_MODULE,
				sizeof(struct pulsar_card), &card);
	if (err < 0)
		return err;

	chip = card->private_data;
	chip->card = card;
	chip->pci = pci;
	chip->irq = -1;
	spin_lock_init(&chip->reg_lock);
	mutex_init(&chip->route_mutex);
	mutex_init(&chip->fifo_mutex);

	err = pcim_enable_device(pci);
	if (err < 0)
		return err;

	pci_set_master(pci);

	err = pcim_iomap_regions(pci, BIT(0), DRV_NAME);
	if (err < 0)
		return err;

	chip->iobase = pcim_iomap_table(pci)[0];
	chip->iobase_phys = pci_resource_start(pci, 0);
	chip->iobase_len = pci_resource_len(pci, 0);

	chip->board_raw_id = readl(chip->iobase + PULSAR_REG_BOARD_ID);
	chip->board_rev = (chip->board_raw_id >> PULSAR_REG_BOARD_REV_SHIFT) &
			  PULSAR_REG_BOARD_REV_MASK;

	dev_info(&pci->dev, "Creamware/SonicCore card at MMIO %pa (len: %llu KB)\n",
		 &chip->iobase_phys, (u64)chip->iobase_len / 1024);
	dev_info(&pci->dev, "Hardware ID: 0x%08x, board revision: %u\n",
		 chip->board_raw_id, chip->board_rev);

	err = dma_set_mask_and_coherent(&pci->dev, DMA_BIT_MASK(32));
	if (err)
		dev_warn(&pci->dev, "Could not set 32-bit DMA mask: %d\n", err);

	err = devm_request_irq(&pci->dev, pci->irq, snd_pulsar_interrupt,
			       IRQF_SHARED, KBUILD_MODNAME, chip);
	if (err < 0) {
		dev_err(&pci->dev, "Cannot grab IRQ %d: %d\n", pci->irq, err);
		return err;
	}
	chip->irq = pci->irq;
	card->sync_irq = chip->irq;

	err = pulsar_hwdep_create(chip);
	if (err < 0)
		return err;

	/* the PCM device is created by PULSAR_IOCTL_SET_ROUTE once the DSP graph is loaded */

	strscpy(card->driver, "Pulsar2", sizeof(card->driver));
	strscpy(card->shortname, "SonicCore Pulsar2", sizeof(card->shortname));
	snprintf(card->longname, sizeof(card->longname),
		 "%s at %pa, irq %d (rev %d)",
		 card->shortname, &chip->iobase_phys, chip->irq, chip->board_rev);

	err = snd_card_register(card);
	if (err < 0)
		return err;

	pci_set_drvdata(pci, card);
	dev++;
	return 0;
}

static int snd_pulsar_probe(struct pci_dev *pci, const struct pci_device_id *pci_id)
{
	return snd_card_free_on_error(&pci->dev, __snd_pulsar_probe(pci, pci_id));
}

/* runs before devres unmaps BAR0: make sure the engine no longer touches our DMA rings */
static void snd_pulsar_remove(struct pci_dev *pci)
{
	struct snd_card *card = pci_get_drvdata(pci);

	if (card)
		pulsar_pcm_quiesce(card->private_data);
}

static struct pci_driver snd_pulsar_driver = {
	.name = DRV_NAME,
	.id_table = snd_pulsar_ids,
	.probe = snd_pulsar_probe,
	.remove = snd_pulsar_remove,
	.shutdown = snd_pulsar_remove,
};

module_pci_driver(snd_pulsar_driver);
