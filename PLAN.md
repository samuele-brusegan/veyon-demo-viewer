# Piano di implementazione — Veyon demo viewer (passive client)

## Obiettivo

Ricevere le presentazioni demo Veyon in una finestra normale (minimizzabile,
ridimensionabile, sempre in primo piano opzionale) **senza** installare
`veyon-service`: niente monitoraggio del nostro schermo, niente controllo
remoto, niente blocco input.

Il demo di Veyon è push unicast: il master ordina ai client registrati
(porta 11100) di connettersi al demo server sulla macchina del docente
(tipicamente porta 11400). Quindi servono due componenti.

## Architettura

```
master docente ──TCP 11100──> fake_service (noi) ──parse DemoClientStart──> demo_viewer
                                                                     │
                                    <────────── TCP 11400 (RFB + token auth)
```

### Componente 1: `fake_service` (demone)

- Ascolta su `0.0.0.0:11100`.
- Handshake RFB stile Veyon: versione, security types, auth (accetta
  qualsiasi metodo offra il master — siamo noi il server, decidiamo noi).
- Scambio info servizio post-auth: risponde con protocol version, utente di
  sessione finto, lista feature supportata.
- Parse dei `FeatureMessage`: cattura `DemoClientStart` (uid demo, comando,
  argomenti host/porta/token). Ignora/rifiuta gentilmente tutto il resto
  (lock, reboot, message box...).
- Keepalive/heartbeat se il protocollo li richiede.
- Opzionale: se il master chiede framebuffer (monitoring thumbnail),
  risponde con frame neri/statici → il docente vede schermo "vuoto", non il
  nostro.
- Logging hex di tutto il traffico per debug.

### Componente 2: `demo_viewer` (finestra)

- Trigger: `DemoClientStart` catturato dal fake service (IPC via socket
  locale o stesso processo asyncio).
- Connessione a `demo_host:demo_port` con autenticazione a token Veyon.
- Client RFB: `ClientInit/ServerInit`, `SetPixelFormat` (32bpp truecolor),
  `SetEncodings` (raw + copyrect + altri scoperti in recon),
  `FramebufferUpdateRequest` incrementale in loop.
- Decode rettangoli → `QImage` in una finestra PyQt. Fullscreen ignorato.

## Milestones

1. **Recon** su sorgente Veyon (`~/development/veyon-src`, clone shallow):
   - `core/src/` — handshake RFB, security types, `FeatureMessage`;
   - `plugins/demo/` — DemoServer/DemoClient, token, porte;
   - `service/` — come il service lancia il demo client.
   Output: `docs/protocol.md` con il wire format documentato.
2. `fake_service.py` — asyncio, handshake + parse feature + logging.
3. `demo_viewer.py` — client RFB + finestra Qt.
4. **Test E2E self-contained**: `veyon-master` + `veyon-service` installati
   in locale, network object su `127.0.0.1:11100` → avvio demo → verifica
   che il fake service riceva il comando e il viewer mostri lo schermo.

## Rischi / punti aperti (da chiarire in recon)

- Formato esatto `FeatureMessage` e serializzazione argomenti (custom
  binario, non QVariant standard?).
- Security type IDs custom di Veyon e framing dei sub-metodi auth.
- Se il demo server richiede solo il token o anche auth utente.
- Eventuale cifratura del canale di controllo in versioni recenti (4.9+).
- Se il master si aspetta risposte di stato dai client ("demo attiva").
- Encodings usati dal demo VNC server.

## Dipendenze previste

- Python 3 + PyQt6 (o tkinter/PySide come fallback) per la finestra.
- Nessuna dipendenza da codice Veyon a runtime: solo protocollo
  reimplementato.
