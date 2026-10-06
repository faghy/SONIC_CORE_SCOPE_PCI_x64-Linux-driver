#include <linux/module.h>
#include <linux/export-internal.h>
#include <linux/compiler.h>

MODULE_INFO(name, KBUILD_MODNAME);

__visible struct module __this_module
__section(".gnu.linkonce.this_module") = {
	.name = KBUILD_MODNAME,
	.init = init_module,
#ifdef CONFIG_MODULE_UNLOAD
	.exit = cleanup_module,
#endif
	.arch = MODULE_ARCH_INIT,
};



static const struct modversion_info ____versions[]
__used __section("__versions") = {
	{ 0xf0fdf6cb, "__stack_chk_fail" },
	{ 0x39ee078b, "_dev_info" },
	{ 0x17b202fb, "snd_card_new" },
	{ 0x4aad0ba1, "_dev_err" },
	{ 0x92d5838e, "request_threaded_irq" },
	{ 0x6541ca23, "snd_pcm_set_ops" },
	{ 0xcefb0c9f, "__mutex_init" },
	{ 0xd1856f88, "pci_iounmap" },
	{ 0x39d2d26b, "snd_device_new" },
	{ 0xb23d3cdc, "snd_pcm_set_managed_buffer_all" },
	{ 0x4f41da84, "_dev_warn" },
	{ 0x45c89c12, "pci_set_master" },
	{ 0xdbd2c436, "param_ops_charp" },
	{ 0x5b8239ca, "__x86_return_thunk" },
	{ 0xdfb2073c, "dma_set_coherent_mask" },
	{ 0x7364c204, "param_ops_bool" },
	{ 0x66644365, "pci_release_regions" },
	{ 0x5f4c8f3, "pci_disable_device" },
	{ 0x7250d225, "dma_set_mask" },
	{ 0x132e9dd3, "snd_pcm_period_elapsed" },
	{ 0xb721dc96, "param_ops_int" },
	{ 0xb5b54b34, "_raw_spin_unlock" },
	{ 0xc1514a3b, "free_irq" },
	{ 0xb34efba6, "pci_enable_device" },
	{ 0x55ecd5b, "pci_iomap" },
	{ 0xb5d73e2b, "snd_pcm_new" },
	{ 0x656e4a6e, "snprintf" },
	{ 0xcf9b0f2f, "snd_card_register" },
	{ 0x578b0c76, "snd_card_free" },
	{ 0xd425ad37, "__pci_register_driver" },
	{ 0xa621b767, "param_array_ops" },
	{ 0x3beba0a7, "pci_request_regions" },
	{ 0xba8fbd64, "_raw_spin_lock" },
	{ 0x1f7a02f0, "pci_unregister_driver" },
	{ 0xbdfb6dbb, "__fentry__" },
	{ 0x7fe2a4c3, "module_layout" },
};

MODULE_INFO(depends, "snd,snd-pcm");

MODULE_ALIAS("pci:v000014B5d00000200sv*sd*bc*sc*i*");
MODULE_ALIAS("pci:v000014B5d00000300sv*sd*bc*sc*i*");
MODULE_ALIAS("pci:v000014B5d00000400sv*sd*bc*sc*i*");
MODULE_ALIAS("pci:v000014B5d00000600sv*sd*bc*sc*i*");
MODULE_ALIAS("pci:v000014B5d00000800sv*sd*bc*sc*i*");
MODULE_ALIAS("pci:v000014B5d00000900sv*sd*bc*sc*i*");
MODULE_ALIAS("pci:v000014B5d00000A00sv*sd*bc*sc*i*");
MODULE_ALIAS("pci:v000014B5d00000B00sv*sd*bc*sc*i*");
