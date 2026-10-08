# Driver Linux per Creamware / Sonic Core Pulsar II (snd-pulsar)

[English](README.md) | **Italiano**

Driver Linux per le schede audio DSP **Creamware / Sonic Core Pulsar II** (PCI `14b5:0600`, 6× SHARC ADSP-21065L).
È sviluppato tramite reverse engineering del software Windows SCOPE 5.1: il driver kernel `scScope.sys` e la libreria `Sim2k.dll`.

## Stato

| Funzione | Stato |
|---|---|
| Rilevamento PCI, mappatura BAR0, IRQ | funziona |
| Avvio dei 6 DSP (caricamento OS `puls2os*.21k`) | **funziona**: ogni DSP risponde |
| Clock audio interno 44,1 kHz | **funziona** (verificato: 44.095 campioni/s) |
| 48 kHz | **funziona** (predefinito, come PipeWire) |
| Clock esterno | implementato, non ancora testato |
| Caricamento moduli DSP (linker), uscite analogiche | **funziona**: tono di prova sulle uscite 1/2 |
| Riproduzione audio dal PC (ALSA PCM, PipeWire) | **funziona**: uscita "Pulsar2 Stereo" |
| Registrazione dagli ingressi analogici 1/2 | **funziona**: ingresso "Pulsar2 Stereo" |
| Monitor diretto ingressi → uscite, volumi in `alsamixer` | **funziona**: "DSP Out", "Input Monitor" |
| Installazione con avvio automatico (DKMS + servizio systemd) | **funziona**: la scheda parte da sola all'accensione |
| Servizio `pulsard`: moduli DSP caricati e collegati a scheda accesa (`pulsarctl`) | **funziona** |
| App grafica "Pulsar Scope" (Qt): libreria moduli, trascina e rilascia, cavi, cursori degli ingressi | **funziona** (prima versione) |
| Progetti: salva/apri (`.pulsar`), banco ripristinato da solo dopo un riavvio | **funziona** |
| Pacchetto `.deb` (driver DKMS, servizio, strumenti, app) | **funziona** |
| Dispositivi SCOPE (`.dev`): 36 effetti (EQ, filtri, dinamica, distorsione, phaser, flanger, chorus…) come un unico blocco con manopole in unità reali | **funziona** |
| Manopole in stile SCOPE (Hz, dB…) per dispositivi e moduli comuni, codifica corretta degli ingressi float/interi | **funziona** |
| Delay e riverberi (linee di ritardo nella RAM del PC, nuovi ioctl del kernel) | **funzionante** (Delay S provato sulla scheda) |
| Dispositivi "Effect Package" con licenza: sbloccati dalla scheda con il **tuo** file di licenza SCOPE (`sudo pulsar-import-dsp --license TUOSERIALE.v5`) | **funzionante**: 107 dispositivi con licenza |
| Preset di fabbrica (`.pre`): menu dei preset nel pannello del dispositivo, `pulsarctl load_preset` | **funzionante** |
| MIDI dal PC (client ALSA sequencer "Pulsar2 MIDI") + synth di prova polifonico (4 voci) | **funzionante**: note e accordi intonati |
| Synth di fabbrica (array di voci), porte ADAT/S/PDIF/MIDI | da fare |

## Architettura

Come su Windows, il driver kernel si limita a esporre l'hardware. L'avvio dei DSP avviene da userspace:

- **`snd-pulsar.ko`**: modulo kernel. Rileva la scheda e gestisce l'IRQ. Il device hwdep `/dev/snd/hwC<n>D0` permette `mmap` della BAR0 (4 MB).
  Il dispositivo PCM ALSA viene creato quando il loader ha caricato i moduli DSP (ioctl `PULSAR_IOCTL_SET_ROUTE`).
- **`tools/pulsar_loader.py`**: reset della scheda, caricamento degli OS dei DSP, avvio e configurazione del clock.
- **`tools/sc_decode.py`**: decodifica i file DSP di SCOPE (`.21k`/`.dsp`/`.ol`, COFF Analog Devices offuscati).
- **`tools/pulsar_modules.py`**: linker dei moduli DSP (rilocazione, caricamento, catena di esecuzione, collegamenti).
- **`tools/scope_dev.py`**: decodifica i file dispositivo di SCOPE (`.io`/`.dev`/`.mdl`/`.pro`).
- **`tools/pulsard.py`** + **`tools/pulsarctl.py`**: servizio che tiene la scheda dopo l'avvio e accetta comandi (`pulsarctl status`, `load`, `connect`, `set`, `unload`) sul socket `/run/pulsard.sock` (gruppo `audio`).
- **`tools/pulsar_scope.py`**: app modulare "Pulsar Scope" (Qt/PySide6, qualsiasi desktop): si trascinano i moduli
  dalla libreria al banco, si collegano le prese con il mouse, si regolano i valori e si salvano/aprono i progetti
  (`.pulsar`). Si avvia dal menu delle applicazioni. Il banco corrente viene anche salvato da solo e ripristinato all'avvio.
- **`tools/scope_device.py`**: trasforma un dispositivo SCOPE (`.dev`) in un piano di montaggio: moduli DSP, cavi
  interni, prese esterne, parametri con unità e curve di SCOPE (`plan`, `survey`, `raw`).
- **`tools/pulsar_values.py`** / **`tools/pulsar_widgets.py`**: codifica dei valori degli ingressi (fixed 1.31, interi,
  float) e la manopola in stile SCOPE usata da Pulsar Scope.
- **`tools/sharc_dis.py`**: disassemblatore SHARC (port di `sharc_dasm.cpp` di MAME, BSD-3).
- **`docs/`**: note di reverse engineering (mappa registri, protocollo dei comandi, sequenza di boot, clock).

## File DSP (non inclusi)

I file DSP sono di proprietà di Sonic Core e **non sono distribuiti** in questo repository.
Servono l'installer ufficiale `SONIC_CORE_SCOPE_PCI_v5.1.2709-x64_EN.exe` e una licenza valida.
Estrai l'installer con `innoextract` in una cartella `scope_full` accanto al repository:
```bash
innoextract -d ../scope_full SONIC_CORE_SCOPE_PCI_v5.1.2709-x64_EN.exe
```
In alternativa puoi indicare la cartella `App/Dsp` con la variabile `PULSAR_DSP_DIR`.

## Installazione dal pacchetto .deb (Debian / Ubuntu)

Scarica `snd-pulsar_<versione>_all.deb` dalla pagina [Releases](https://github.com/faghy/SONIC_CORE_SCOPE_PCI_x64-Linux-driver/releases), poi:
```bash
sudo apt install ./snd-pulsar_0.5.1_all.deb                          # driver (DKMS), servizio, strumenti, Pulsar Scope
sudo pulsar-import-dsp SONIC_CORE_SCOPE_PCI_v5.1.2709-x64_EN.exe      # una volta: file DSP e dispositivi di Sonic Core
```
Riavvia (o ricarica il modulo) e la scheda parte da sola. Si rimuove con `sudo apt remove snd-pulsar`
(`purge` cancella anche i file DSP importati e il banco salvato). Il pacchetto si costruisce con
`packaging/build-deb.sh`.

## Installazione dai sorgenti (avvio automatico)

```bash
sudo ./install.sh --dsp-from ../scope_full/app/App/Dsp     # oppure --dsp-from SONIC_CORE_SCOPE_PCI_v5.1.2709-x64_EN.exe
```
Lo script installa le dipendenze (`dkms`, header del kernel) e il driver tramite DKMS, che lo ricompila a ogni
aggiornamento del kernel. Installa anche gli strumenti in `/usr/lib/snd-pulsar`, i file DSP in
`/var/lib/snd-pulsar/dsp` e il servizio `snd-pulsar@.service`. Il servizio parte da solo quando la scheda viene
rilevata e avvia DSP, clock, audio e monitor. Le impostazioni stanno in `/etc/default/snd-pulsar`, i log si leggono
con `journalctl -u 'snd-pulsar@*'`. Disinstallazione: `sudo ./uninstall.sh` (`--purge` rimuove anche i file DSP).

## Compilazione e test

```bash
make                                   # usa /lib/modules/$(uname -r)/build
pulsarctl status                       # con il servizio installato: stato della scheda e dei moduli
pkexec tools/pulsar_test.sh boot       # carica il modulo, avvia i DSP e il clock
pkexec tools/pulsar_test.sh clock      # misura il word clock dei DSP
pkexec tools/pulsar_test.sh boot --tone 440 --volume -40   # tono di prova sulle uscite analogiche 1/2
pkexec tools/pulsar_test.sh reload boot --bus-master --irq --pcm --monitor -12   # scheda audio ALSA/PipeWire + monitor
alsamixer -c Pulsar2                   # volumi "DSP Out" e "Input Monitor"
tools/pulsar_loader.py boot --dry-run  # simulazione senza hardware
```
Altri comandi di `pulsar_test.sh` (tutti richiedono root):
- `info`: legge registri e stato della scheda;
- `diag`: diagnostica sullo stato del bus tra i DSP e della SRAM condivisa;
- `dump`: salva su file i registri e la SRAM della scheda;
- `peek --dsp N --sym NOME`: legge una variabile dall'OS di un DSP in esecuzione;
- `clock`: misura il word clock dei DSP.

## Licenza

GPL-2.0-or-later (driver kernel). `tools/sharc_dis.py` deriva da MAME (BSD-3-Clause).
