# Investigação: live view RTSP caiu para polling de snapshot

**Data:** 2026-06-16
**Device:** HVR XMEye, 9 canais, host `192.168.16.10` (DVRIP porta 34567, RTSP 554)
**Status:** Causa raiz identificada e confirmada. Correção pendente (escolha de remediação).

---

## 1. Sintoma

No card de câmera do Home Assistant, o live view:
- carrega o snapshot, fica um tempão "pensando";
- começa a vir **uma imagem a cada ~1s, sem áudio**, em baixa resolução (≈800×448).

Antes (relato do usuário) vinha em **resolução nativa, com áudio e bom framerate**.
O usuário associou a quebra às tentativas recentes de arrumar PTZ.

Comportamento real: o HA tenta abrir o stream, falha em reproduzir, e cai no
**still-image proxy** (busca repetida de `async_camera_image`, snapshot HTTP), que
funciona independente do resto → daí "1 img/s sem áudio".

---

## 2. O que foi PROVADO

### 2.1 A câmera/URL/senha/stream estão 100% OK
Teste direto com `ffprobe` (TCP) contra a câmera:
- **main** `channel=5&stream=0.sdp` → **HEVC (H.265) 2880×1616** + áudio PCMA.
- **sub**  `channel=5&stream=1.sdp` → **HEVC (H.265) 800×448** + áudio PCMA.
- Autenticação é exigida de verdade: senha errada (plain ou hash) → **401**; vazia → 401.
- A câmera aceita **tanto a senha em texto puro (`ma56ter`) quanto o `sofia_hash`
  (`W6xpk6c9`)** — as duas representações da senha correta são válidas. `sofia_hash`
  **não** está quebrado.

URL principal que a integração gera (e que funciona):
```
rtsp://192.168.16.10:554/user=admin&password=W6xpk6c9&channel=5&stream=0.sdp
```

### 2.2 A conexão DVRIP está estável (NÃO há loop de reconexão)
- Entidade `camera.hvr_ch5` ("HVR Externa") em estado **`idle`** (não `unavailable`),
  `supported_features: 2` (STREAM) → **`coordinator.connected = True`, estável.**
- Os ciclos `connect/login/storage` a cada 60s no log **não são reconexões**: são o
  `_async_refresh_storage` (timer de 60s) abrindo conexões curtas (`async_run_command`)
  que só fazem storage e fecham. Por isso truncam após `HDD capacity` e **não geram
  WARNING**. A conexão persistente (sessão B3) subscreveu e seguiu viva no `read_events`.

### 2.3 O backend do HA abre o stream sem erro
Com `homeassistant.components.stream: debug`:
```
22:13:06 [...stream.camera.hvr_ch1] Started stream: rtsp://...channel=1&stream=0.sdp
22:13:46 [...stream.camera.hvr_ch1] Stopped stream: rtsp://...channel=1&stream=0.sdp   (~40s)
22:16:04 [...stream.camera.hvr_ch2] Started stream: rtsp://...channel=2&stream=0.sdp
22:16:43 [...stream.camera.hvr_ch2] Stopped stream: rtsp://...channel=2&stream=0.sdp   (~39s)
```
- `Started stream` (sem erro de ffmpeg) = o backend conectou no RTSP HEVC e o worker
  HLS rodou. **Servidor OK.**
- `Stopped stream` ~40s depois, **sozinho, antes de o usuário fechar o card** =
  **idle-timeout**: o navegador não consome os segmentos HLS (não decodifica HEVC),
  então o HA mata o worker. O vídeo **nunca chegou a tocar**.
- **Nenhuma linha de `homeassistant.components.go2rtc`** apareceu → **go2rtc não está
  ativo**; o HA usa o caminho `stream` legado (HLS).

---

## 3. Causa raiz

**A câmera só entrega H.265 (HEVC) em ambos os streams, e o navegador usado (Linux:
Chrome/Chromium/Firefox) não decodifica HEVC via HLS/MSE.** O HA produz HLS com vídeo
HEVC; o navegador recusa; o card cai no still-image proxy (snapshot ~1fps).

- No **Linux**, suporte a HEVC em navegadores é praticamente inexistente/instável
  (Chrome não traz decoder de SW; VA-API é experimental e não cobre HLS/MSE; Firefox
  não tem; Edge no Linux não é confiável).
- **A integração está inocente:** o código de streaming não mudou, o backend abre o
  HEVC perfeitamente, e a câmera sempre foi H.265. A coincidência com o trabalho de
  PTZ é temporal; o que mudou foi a capacidade de **decodificar HEVC no
  navegador/OS** (provável: o "funcionava antes" era em outro viewer — app móvel,
  Safari/iOS, ou player externo).

### Confirmação visual sugerida (gargalo é só o navegador)
```bash
vlc "rtsp://192.168.16.10:554/user=admin&password=W6xpk6c9&channel=2&stream=0.sdp"
mpv "rtsp://192.168.16.10:554/user=admin&password=W6xpk6c9&channel=2&stream=0.sdp"
```
Toca liso, nativo, com áudio → prova que é exclusivamente o navegador + HEVC.

---

## 4. Hipóteses descartadas (para não repetir)

- **Loop de reconexão / `connected` instável** — REFUTADO. `connected=True` estável
  (entidade `idle`); os "reconnects" eram só o refresh de storage de 60s.
- **`channel_title` (cmd 1048) derrubando a conexão persistente** — REFUTADO. No 1º
  ciclo o cmd 1048 funcionou e retornou os nomes
  `['Entrada','Sala','D03','HIPC','Externa','D06','D07','D08','D09']`.
- **`sofia_hash` errado / URL RTSP errada** — REFUTADO. URL com `sofia_hash` autentica
  e entrega vídeo; backend dá `Started stream` sem erro.
- **Guard `if not self._coordinator.connected: return None` em `stream_source`** — não
  é o causador aqui (connected=True). Continua sendo um acoplamento desnecessário
  (RTSP 554 é independente do socket de alarme 34567) e vale como hardening futuro,
  mas NÃO é a causa deste problema.

---

## 5. Remediação (todas fora do código de streaming da integração)

A integração não pode transcodar (isso é go2rtc/ffmpeg). Caminhos reais no Linux:

### Opção A — Transcodificar H.265→H.264 via go2rtc (escolha atual do usuário)
Mantém gravação em H.265; gasta CPU do host; funciona em qualquer navegador.

1. Instalar **WebRTC Camera (AlexxIT)** via HACS (embute go2rtc e lê `go2rtc.yaml`).
   (O go2rtc embutido do HA também serve, mas customizar a config dele é mais chato.)
2. `/config/go2rtc.yaml` — **transcodar o substream** (800×448, baratíssimo) e deixar
   o main H.265 para snapshot/gravação:
   ```yaml
   streams:
     hvr_ch5:
       - rtsp://192.168.16.10:554/user=admin&password=W6xpk6c9&channel=5&stream=1.sdp
       - ffmpeg:hvr_ch5#video=h264#audio=copy
   ```
   Com HW (VAAPI/QSV/NVENC): `ffmpeg:hvr_ch5#video=h264#hardware`.
3. Card:
   ```yaml
   type: custom:webrtc-camera
   url: hvr_ch5
   ```

### Opção B — Trocar o encoder da câmera para H.264
No app XMEye / web da câmera. Mais simples no HA, compatível com tudo, mas afeta
gravação/compressão. (Pode ser feito pela integração — ver §6.)

### Opção C — Reabilitar HEVC no navegador/OS
Inviável de forma confiável no Linux. (Em Windows: "HEVC Video Extensions"; em
Mac/iOS: Safari decodifica nativo.)

---

## 6. Como a integração poderia FACILITAR (roadmap)

A integração não transcoda, mas pode ajudar bastante:

1. **⭐ Seleção de stream ciente de codec (alto valor amplo).**
   Estender `_find_stream_url`/`_rtsp_has_video` (`camera.py:302,111`) para ler o codec
   no SDP (`m=video ... H264/H265`) e **preferir um substream H.264** no live view
   quando existir, mantendo o main para snapshot/gravação. Resolve a maioria dos
   devices XMEye cujo sub é/pode ser H.264. (Não ajuda este HVR — ambos H.265.)

2. **⭐⭐ Serviço/opção para setar o codec do device para H.264.**
   Reusar o acesso ao `Simplify.Encode` que já existe (`async_get_encode_cfg`,
   `async_set_recording_enabled` em `coordinator.py:227,251`). Campo:
   `[canal].MainFormat/ExtraFormat.Video.Compression` = `"H.264"`/`"H.265"`. Expor
   `xmeye.set_codec` (ou um `select` por câmera) que coloca o **substream** em H.264 →
   live view via `stream=1` toca nativo em qualquer navegador, **sem go2rtc, sem CPU
   de transcode**, mantendo o main em H.265 para gravação.

3. **Detecção de HEVC + Repair issue.**
   Quando o probe detectar live stream H.265, criar um issue no `issue_registry`
   (ou notificação persistente) avisando que a maioria dos navegadores não toca H.265 e
   apontando para go2rtc/troca de codec. Transforma o "não funciona e não sei por quê"
   em aviso acionável.

**Sugestão de priorização:** #1 + #3 no curto prazo (impacto amplo, baixo risco); #2 no
médio prazo (para devices H.265 nos dois streams, como este).

---

## 7. Pendências / limpezas correlatas (separadas deste problema)

- **Ruído de storage 607:** `config_get(StorageDeviceInfo/StorageInfo/Storage)` falha
  com Ret=607 neste HVR; o `_async_refresh_storage` (a cada 60s) keeps relogando e
  falhando. Candidato a desativar/condicionar (parar de tentar após N falhas) num PR
  de limpeza. Não afeta o stream, mas polui o log e abre conexões à toa.
- **Hardening `stream_source`:** remover o guard `if not connected` (desacoplar do
  socket de alarme) como defesa em profundidade. Não é a causa deste caso.
- **Já corrigido (PR #16 / v0.5.1):** thread-safety do `_do_clear` (motion debounce)
  chamando `async_write_ha_state` fora do event loop. Bug separado, descoberto durante
  esta investigação.
```

---

## 8. Resolução (v0.8.0) — por que o registro no go2rtc não pegava

Investigação de 2026-07-26, lendo o fonte do HA Core (`dev`) e do go2rtc 1.9.14.
Três causas encadeadas, todas corrigidas em `fix/go2rtc-h265-ha-identifier`.

### 8.1. A API do go2rtc do HA era inalcançável

`homeassistant/components/go2rtc/server.py` escreve `api.listen: ""` quando
`debug_ui` não está ligado — **não existe porta TCP**. A API fica só no unix socket
`<mkdtemp go2rtc-*>/go2rtc.sock`, com `local_auth: true` e usuário/senha gerados a cada
start. O `Go2RTCClient` antigo sondava `127.0.0.1:{11984,1984}` sem credencial e aceitava
qualquer `status < 500` — um 401 contava como "disponível", e o `PUT` seguinte falhava.

Correção: reaproveitar a `ClientSession` e a URL que a própria integração `go2rtc` montou
(`hass.data["go2rtc"]` → `Go2RtcConfig(url, session)`), que já carregam o `UnixConnector`
ou o TCP e o BasicAuth certos. Cobre de uma vez o binário gerido pelo HA, o `debug_ui`
ligado e a instância externa (`go2rtc: url:`). A sondagem de portas fica só como fallback,
agora exigindo `200`.

### 8.2. Nome do stream errado

`homeassistant/components/go2rtc/util.py::get_camera_identifier` usa
`f"{platform_name}_{unique_id}"` → `xmeye_<entry_id>_ch<N>_camera`. A integração
registrava `xmeye_<entry_id>_ch<N+1>`. Eram dois streams distintos: o nosso, com o
producer `ffmpeg:…#video=h264`, nunca era consumido; o do HA — cujo segundo source é
`ffmpeg:<id>#audio=opus#query=log_level=debug`, **só áudio** — era o que chegava ao
browser. O HA nunca gera `#video=h264` a não ser no caminho de orientação de câmera.

### 8.3. O salto RTSP extra colapsava a negociação

`stream_source()` devolvia `rtsp://127.0.0.1:18554/<nosso stream>`, fazendo o HA criar um
segundo stream que puxava o nosso por loopback. Em `internal/streams/add_consumer.go` o
consumidor RTSP aceita o primeiro media que casa e faz `break producers` — ou seja, pega o
H.265 do producer[0] e o ffmpeg nunca é ativado. A negociação multi-source só funciona com
o consumidor final (o WebRTC do browser) ligado direto ao stream multi-source.

### 8.4. A alavanca

`WebRTCProvider._update_stream_source()` só reescreve o stream quando:

```python
if (stream := streams.get(identifier)) is None or not any(
    stream_source == producer.url for producer in stream.producers
):
```

e chama `await camera.stream_source()` **imediatamente antes** desse `if`. Registrando sob
o identificador do HA, com o source[0] idêntico ao que `stream_source()` devolve, o HA pula
a reescrita; e re-afirmando o registro dentro do próprio `stream_source()` o estado se
auto-corrige caso algo o substitua.

A comparação é confiável: em go2rtc, `Producer.MarshalJSON` devolve `{"url": p.url}` quando
ocioso e o `core.Connection` quando ligado, cujo campo `url` vem de
`pkg/rtsp/client.go:78` (`c.Connection.URL = c.uri`) — a string do source, verbatim.

### 8.5. Sobre trocar o codec no DVR

Continua sendo o melhor resultado em consumo (zero transcoding), mas **a integração não
mexe no encoder do usuário**. Passou a haver dois repair issues por device (não por canal):
`h265_transcoding` quando o go2rtc está convertendo, `h265_no_go2rtc` quando não consegue.
Ambos explicam o trade-off: H.264 ocupa ~40–100 % mais disco por minuto que H.265 com
qualidade equivalente (perto do dobro em cenas com muito movimento; ~15–25 % em baixa
resolução ou cenas quase estáticas).

---

## 9. Follow-up (v0.8.1) — o provider WebRTC nunca era anexado

Com a v0.8.0 o registro no go2rtc passou a acontecer corretamente (config confirmada em
campo, os três canais H.265 com os dois producers cada), mas **nenhum cliente consumia via
go2rtc**: Companion abria em H.265 direto e Chrome caía no still-image proxy (~1 fps), os
dois com alguns segundos de espera — assinatura clássica do caminho HLS.

Causa: `Camera.async_refresh_providers()` só roda em `async_internal_added_to_hass()`, e
quando um provider se registra/desregistra, ou quando o bit `CameraEntityFeature.STREAM`
muda. Nesse primeiro momento:

- `XMEyeCoordinator.async_setup()` dispara `_connection_loop()` como background task e
  retorna imediatamente, então `coordinator.connected` ainda é `False`;
- `stream_source()` tinha o guard `if not self._coordinator.connected: return None`;
- `async_get_supported_provider()` recebe `None` e devolve `None` → `_webrtc_provider`
  fica `None` → `camera_capabilities` = `{HLS}` para o resto da vida da entidade.

Ironia: o `after_dependencies: [go2rtc]` adicionado na v0.8.0 piorou isso. Antes, se a
integração `go2rtc` subisse depois das câmeras, o `async_register_webrtc_provider()` dela
disparava `_async_refresh_providers(hass)` e dava uma segunda chance de anexar o provider.

Correções:

1. Removido o guard `connected` de `stream_source()` — o RTSP não passa pelo socket DVRIP
   de alarme (era a pendência da §7).
2. `await self.async_refresh_providers()` no fim de `_probe_urls()`, quando já se conhece
   a URL e o codec reais.
3. `_handle_update()` sobrescrito para re-avaliar providers quando o modo privacidade é
   ligado/desligado — privacidade zera `stream_source()`, e o HA não reavalia sozinho.

Como verificar rapidamente, no console do browser com o frontend do HA aberto:

```js
await document.querySelector("home-assistant").hass
  .callWS({type: "camera/capabilities", entity_id: "camera.<sua_camera>"})
```

Antes: `{frontend_stream_types: ["hls"]}`. Depois: `["hls", "webrtc"]`.
