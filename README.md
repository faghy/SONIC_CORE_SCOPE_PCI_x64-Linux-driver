# Driver Linux ALSA per Creamware / Sonic Core Pulsar 2 (snd-pulsar)

Questo progetto contiene il driver kernel Linux nativo per le schede audio DSP **Creamware / Sonic Core Pulsar II e SCOPE**, sviluppato tramite reverse engineering del driver Windows x64 originale (`scScope.sys`).

---

## 1. Architettura Hardware e Registri Identificati

Dall'analisi del driver Windows 64-bit e dall'hardware rilevato sul sistema:

* **PCI Vendor ID:** `0x14B5` (Creamware GmbH)
* **PCI Device ID:** `0x0600` (Pulsar 2)
* **Spazio MMIO (BAR 0):** Finestra non-prefetchable a 32-bit di **4 MB** all'indirizzo fisico `0xf7800000`.
* **Registro di Stato / Revisione Hardware (Offset `0x000000`):**
  * `u32 raw_id = readl(iobase + 0x00);`
  * `u8 board_rev = (raw_id >> 8) & 0x1f;`
* **Finestra FIFO / Mailbox DSP (Offset `0x080000`):**
  * Finestra a 512 KB per lo scambio messaggi con l'array dei 6 DSP SHARC (ADSP-21065L).
* **Protocollo Pacchetti Comandi DSP:**
  * `Header = (cmd << 26) | (len << 22) | 0x000a0000 | (addr & 0x1ffff)`

---

## 2. Struttura del Codice Sorgente

* `pulsar.h`: Definizioni PCI ID, maschere dei registri MMIO, costanti di timing e strutture dati del driver.
* `pulsar_core.c`: Modulo PCI Linux (`pci_driver`), mappatura BAR 0, aggancio IRQ e registrazione della scheda nel sottosistema ALSA (`snd_card`).
* `pulsar_pcm.c`: Interfaccia streaming audio ALSA PCM (Playback & Capture) con allocazione buffer DMA a 32-bit managed.
* `Makefile`: File di compilazione Kbuild compatibile con kernel Linux 5.x / 6.x.

---

## 3. Come Testare il Driver su questo Sistema

Il modulo **`snd-pulsar.ko`** è già stato compilato con successo specificamente per il tuo kernel corrente (`6.12.111+deb13-amd64`).

### Caricamento del modulo:
```bash
sudo insmod /home/faghy/puksar2/linux-driver/snd-pulsar.ko
```

### Verifica del riconoscimento hardware:
```bash
dmesg | grep -i pulsar
```
Dovresti vedere l'output del driver con il rilevamento del chip MMIO a `0xf7800000` e la revisione hardware della scheda.

### Verifica della scheda audio in ALSA:
```bash
cat /proc/asound/cards
aplay -l
```

### Rimozione del modulo:
```bash
sudo rmmod snd-pulsar
```
