// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Creamware / Sonic Core Pulsar 2 hwdep interface: BAR0 mmap + info ioctl,
 * used by the userspace DSP loader.
 */

#include <linux/mm.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/uaccess.h>
#include <sound/core.h>
#include <sound/hwdep.h>

#include "pulsar.h"

static int pulsar_hwdep_open(struct snd_hwdep *hw, struct file *file)
{
	if (!capable(CAP_SYS_RAWIO))
		return -EPERM;
	return 0;
}

static int pulsar_hwdep_ioctl(struct snd_hwdep *hw, struct file *file,
			      unsigned int cmd, unsigned long arg)
{
	struct pulsar_card *chip = hw->private_data;
	struct pulsar_info info = {};
	struct pulsar_pcm_route route;
	struct pulsar_controls *ctls;
	struct pulsar_msg *msg;
	struct pulsar_delay_alloc dalloc;
	struct pulsar_delay_param dparam;
	u32 handle;
	int err;

	switch (cmd) {
	case PULSAR_IOCTL_SET_ROUTE:
		if (copy_from_user(&route, (void __user *)arg, sizeof(route)))
			return -EFAULT;
		return pulsar_pcm_set_route(chip, &route);
	case PULSAR_IOCTL_SET_CONTROLS:
		ctls = memdup_user((void __user *)arg, sizeof(*ctls));
		if (IS_ERR(ctls))
			return PTR_ERR(ctls);
		err = pulsar_mixer_set_controls(chip, ctls);
		kfree(ctls);
		return err;
	case PULSAR_IOCTL_SEND_MSG:
		msg = memdup_user((void __user *)arg, sizeof(*msg));
		if (IS_ERR(msg))
			return PTR_ERR(msg);
		err = msg->count > PULSAR_MSG_MAX_WORDS ? -EINVAL : pulsar_dsp_send(chip, msg->words, msg->count);
		kfree(msg);
		return err;
	case PULSAR_IOCTL_DELAY_ALLOC:
		if (copy_from_user(&dalloc, (void __user *)arg, sizeof(dalloc)))
			return -EFAULT;
		err = pulsar_delay_alloc(chip, &dalloc);
		if (!err && copy_to_user((void __user *)arg, &dalloc, sizeof(dalloc)))
			err = -EFAULT;
		return err;
	case PULSAR_IOCTL_DELAY_FREE:
		if (get_user(handle, (u32 __user *)arg))
			return -EFAULT;
		return pulsar_delay_free(chip, handle);
	case PULSAR_IOCTL_DELAY_PARAM:
		if (copy_from_user(&dparam, (void __user *)arg, sizeof(dparam)))
			return -EFAULT;
		return pulsar_delay_param(chip, &dparam);
	case PULSAR_IOCTL_GET_INFO:
		info.board_raw_id = chip->board_raw_id;
		info.board_rev = chip->board_rev;
		info.bar_len = chip->iobase_len;
		info.irq_count = atomic_read(&chip->irq_count);
		info.last_int_status = READ_ONCE(chip->last_int_status);
		if (copy_to_user((void __user *)arg, &info, sizeof(info)))
			return -EFAULT;
		return 0;
	default:
		return -ENOTTY;
	}
}

/* the delay lines belong to the process that built the DSP graph (pulsard) */
static int pulsar_hwdep_release(struct snd_hwdep *hw, struct file *file)
{
	pulsar_delay_free_all(hw->private_data);
	return 0;
}

static int pulsar_hwdep_mmap(struct snd_hwdep *hw, struct file *file,
			     struct vm_area_struct *vma)
{
	struct pulsar_card *chip = hw->private_data;

	vma->vm_page_prot = pgprot_noncached(vma->vm_page_prot);
	return vm_iomap_memory(vma, chip->iobase_phys, chip->iobase_len);
}

int pulsar_hwdep_create(struct pulsar_card *chip)
{
	struct snd_hwdep *hw;
	int err;

	err = snd_hwdep_new(chip->card, "Pulsar DSP", 0, &hw);
	if (err < 0)
		return err;

	strscpy(hw->name, "Pulsar2 DSP host port", sizeof(hw->name));
	hw->private_data = chip;
	hw->ops.open = pulsar_hwdep_open;
	hw->ops.release = pulsar_hwdep_release;
	hw->ops.ioctl = pulsar_hwdep_ioctl;
	hw->ops.ioctl_compat = pulsar_hwdep_ioctl;
	hw->ops.mmap = pulsar_hwdep_mmap;
	hw->exclusive = 1;
	chip->hwdep = hw;
	return 0;
}
