# Protocollo Veyon — note reverse-engineered (sorgente 4.9.x/master)

## Framing base

- RFB standard per versione/security: server invia `RFB 003.008\n` (12B),
  client risponde identico (major 3, minor 7..8).
- `VariantArrayMessage` (VAM): `u32be size` + payload. Payload = sequenza di
  QVariant serializzati `QDataStream` versione `Qt_5_5`.
- Serializzazione QVariant: `u32be typeId` + `u8 isNull` + payload tipo.
  - Bool=1: i8
  - Int=2: i32
  - LongLong=4: i64
  - QVariantMap=8: u32 n, poi n×(QString key, QVariant value)
  - QVariantList=9: u32 n, poi n×QVariant
  - QString=10: u32 byteLen (0xffffffff=null) + UTF-16BE
  - QStringList=11: u32 n + n×QString
  - QByteArray=12: u32 len (0xffffffff=null) + bytes
  - QRect=19: 4×i32 (x,y,w,h)
  - QUuid=30: 16B (u32,u16,u16,8×u8, tutti BE)
  - tipi "user" (es. enum `RfbVeyonAuth::Type`): typeId registrato + payload
    via QMetaType::save (enum → i32). Verificare empiricamente; per scrittura
    usare Int semplice, il lato Qt converte.

## Connessione master → servizio (porta 11100)

Ruoli: NOI = server, master = client.

1. noi → `RFB 003.008\n`; master → `RFB 003.008\n`
2. noi → `[0x01, 0x28]` (1 security type, `rfbSecTypeVeyon` = 40)
3. master → `0x28`
4. noi → VAM `[Int nTypes][authType]*` — auth types: KeyFile=3, Logon=4
   (offrire entrambi)
5. master → VAM `[chosenAuthType, QString username]`
6. noi → VAM vuota (ack), poi scambio auth:
   - **KeyFile**: noi → VAM `[challenge bytearray]` (qualunque blob, ~128B).
     master → VAM `[keyName QString, signature bytearray]`. Non verifichiamo.
   - **Logon**: noi → VAM `[pubkey PEM]` (RSA generata da noi). master → VAM
     `[password cifrata bytearray]`. Non decriptiamo.
7. noi → `u32be 0` (`rfbVncAuthOK`)
8. AccessControl: nessun traffico se approviamo subito (default).
9. master → ClientInit 1B (shared)
10. noi → ServerInit `u16 w, u16 h, pixelFormat 16B, u32 nameLen, name`
    (pixelformat qualsiasi; il master poi fa SetPixelFormat)
11. Running:
    - client→server: msg RFB standard (0 SetPixelFormat, 1 FixColourMap,
      2 SetEncodings, 3 FBUR(10B), 4 KeyEvent(8B), 5 PointerEvent(6B),
      6 ClientCutText(8B+text), 250 Xvp, 251 SetDesktopSize) **e** tipo 41 =
      FeatureMessage `[0x29][VAM[QUuid uid, i32 cmd, QVariantMap args]]`
    - server→client: 0 FbUpdate, 1 SetColourMapEntries(6B), 2 Bell(1B),
      3 ServerCutText(8B+text), 4 ResizeFrameBuffer, 250 Xvp; tipo 41 =
      FeatureMessage (reply/async dal servizio)
    - Noi: a ogni FBUR rispondiamo FB update con rect raw neri (onorare il
      pixelformat richiesto dal master) → thumbnail nera, host "online".

## FeatureMessage demo (master → noi)

- uid fullscreen `7b6231bd-eb89-45d3-af32-f70663b2f878`,
  window `ae45c3db-dc2e-4204-ae8b-374cdab8c62c`
- comandi: StartDemoClient=2, StopDemoClient=3
- args (chiavi = indice enum `Argument` come stringa):
  - `"0"` DemoAccessToken (QByteArray)
  - `"3"` DemoServerHost (QString; VUOTO per demo del docente → usare
    l'IP del master che ha mandato il messaggio)
  - `"4"` DemoServerPort (Int; default 11400+sessionId)
  - `"5"` Viewport (QRect)
- Stop: uid + cmd 3 (su entrambi gli uid).

## Demo server (noi → host:porta, tipicamente :11400)

Stesso handshake Veyon; `supportedAuthTypes = [Token=5]`:

1. RFB ver; sectypes `[1,40]`; rispondiamo `0x28`
2. server → VAM `[Int 1][Token]`; noi → VAM `[Token, ""]`
3. server → VAM vuota (ack); stato auth Init→Token senza messaggi
4. noi → VAM `[token bytearray]`; server → `u32be 0` (rfbVncAuthOK)
5. noi → ClientInit 1B; server → ServerInit
6. RFB running: mandiamo SetPixelFormat (32bpp truecolor BGRX),
   SetEncodings `[raw=0, copyrect=1]`, FBUR non-incrementale poi
   incrementale in loop; decodiamo rect raw/copyrect (+pseudo
   LastRect=0xffffffe0 come terminatore, DesktopSize/pseudo ignorati).

## Note operative

- Nessuna cifratura sul canale (solo RSA dentro l'auth).
- Il servizio reale è un proxy verso VNC interno (localhost, password
  interna) — per noi irrilevante, non replichiamo il backend.
- FeatureMessage sconosciuti (lock, reboot, textmessage...): log + ignora.
- Il master si aspetta possibili FeatureMessage async dal servizio
  (stato feature/utente): non inviarli = utente mostrato sconosciuto,
  ma la demo funziona comunque.
