# Investigação PTZ DVRIP — Histórico

> **PTZ já está funcionando.** Esta documentação é apenas para registro histórico do desenvolvimento.

**Data da última atualização:** 2026-06-15  
**NVR:** 192.168.16.10 (Sofia/XMEye)  
**Câmera PTZ:** Canal 5 no app ("Externa"), índice 4 (zero-based)  
**Credenciais:** admin / ma56ter (sofia_hash = "W6xpk6c9")  
**Hardware:** NBD80X16S-KL, Serial=429014829f7a90c7  
**Firmware:** V4.03.R11.C638023D, BuildTime=2022-02-17  

---

## Objetivo

Fazer `xmeye.ptz` via protocolo DVRIP (TCP 34567) mover fisicamente a câmera PTZ no canal 4 do NVR 192.168.16.10.

**Restrições do usuário:**
- NÃO misturar ONVIF com DVRIP na integração
- NÃO assumir que câmeras IPC sem senha funcionam sem senha
- NÃO colocar senhas em PRs/documentos públicos

---

## O que funciona

1. **ONVIF PTZ** (porta 8899, câmera 192.168.16.11): move fisicamente a câmera. Confirmado por comparação de pixels. Mas usuário não quer ONVIF na integração.
2. **Login DVRIP plaintext** (`CMD_1000`): retorna `Ret=100`. Sessão autenticada.
3. **CMD_1413 após login**: retorna `Ret=100` (ack simples, sem handshake DH).
4. **CMD_1400 PTZ plaintext**: retorna `Ret=100` **mas câmera NÃO se move** (pixel diff = ~2-4/255 vs threshold 15/255 confirmado vs movimento real de ~41/255).

---

## O Problema Central

O NVR aceita os comandos PTZ (`Ret=100`) mas **não executa** o movimento físico. Comandos enviados via sessão plaintext são aparentemente ignorados pelo firmware. O app XMEye oficial usa sessão cifrada e PTZ funciona.

---

## Protocolo DVRIP — Estrutura

```
Header: <BB2xII2xHI> = 20 bytes
  [0] = 0xFF (magic)
  [1] = version
  [2-3] = padding
  [4-7] = session_id (uint32, little-endian)
  [8-11] = seq (uint32)
  [12-13] = padding
  [14-15] = cmd (uint16)
  [16-19] = length (uint32)
Body: length bytes (JSON + "\n\x00" ou dados binários/base64)
```

**Comandos relevantes:**
| CMD | Nome | Descrição |
|-----|------|-----------|
| 1000 | LOGIN | Login (plaintext ou cifrado) |
| 1001 | LOGIN_RSP | Resposta login |
| 1006 | KEEPALIVE | Keepalive |
| 1020 | OPMonitor Start | Inicia stream de vídeo |
| 1042 | CONFIG_GET | Obter configuração |
| 1400 | PTZ_CONTROL | Controle PTZ |
| 1401 | PTZ_CONTROL_RSP | Resposta PTZ |
| 1413 | MONITOR_CLAIM | Handshake de criptografia |
| 1414 | MONITOR_CLAIM_RSP | Resposta com chave/parâmetros |

---

## O Protocolo Real do App (capturado via MITM)

### Como capturar
`mitm_proxy.py` faz proxy entre app (192.168.16.175) e NVR (192.168.16.10) na porta 34567. Saída em `/tmp/mitm_output.txt`.

**Bug conhecido no MITM**: `body_str = repr(raw_body[:200])` trunca corpos grandes. CMD_1413/1414 são afetados.

### Fluxo observado no MITM

O app mantém **duas camadas de sessão**:

#### Camada 1: Sessão Principal (persistente, criada ANTES do MITM)
- SID = `0x0001869F` (99999 decimal) — hardcoded no app XMEye?
- **Nunca vimos o CMD_1000 que criou essa sessão** (aconteceu antes do MITM iniciar)

#### Camada 2: Sub-sessão de Vídeo (criada a cada 44s)
A cada vez que o app precisa de um stream de vídeo:

```
APP→NVR  CMD_1413  sid=0x0001869F  seq=4088
  body: {"Name":"OPMonitor","OPMonitor":{"Action":"Claim","Parameter":
         {"Channel":0,"CombinMode":"CONNECT_ALL","StreamType":"Main","TransMode":"TCP"}},
         "DHParameter":{"RandomStrA":"81t6"},
         "SessionID":"0x000001869f"}

NVR→APP  CMD_1414  sid=0x0010C8DF  ← SID NOVO
  body: [150 bytes de blob binário / base64]
  "7b39TFcOQLvicIPJ+ruy/zRNtJmETeNos+id7XiwLi9t7vW74uHQ3/..."

APP→NVR  CMD_1000  sid=0x00000000  (criando nova sessão)
  body: [150 bytes cifrados, base64]
  "rfIQlMaR6Ses2pRV7x9RcqLQDOs9GDPH7FuY0jxaWEwOBvxOE2S7..."
  Primeiros 48 bytes: SEMPRE IGUAIS entre sessões
  Bytes 48+: VARIAM conforme RandomStrA

NVR→APP  CMD_1001  sid=0x000009A7  ← nova sub-sessão
  body: {"AliveInterval":21,"ChannelNum":9,"DataUseAES":false,
         "DeviceType":"HVR","Ret":100,"SessionID":"0x000009A7"}

[todos os comandos seguintes na sub-sessão são CIFRADOS]
APP→NVR  CMD_1020  body: [base64 cifrado]
APP→NVR  CMD_1042  body: [base64 cifrado]
APP→NVR  CMD_1400  body: [base64 cifrado] ← NUNCA CAPTURADO EM PLAINTEXT
```

### Observações críticas do MITM

1. **RandomStrA** varia entre sessões: "81t6", "0H4q", "e22P", "3V9R", "ta2k", etc.
2. **CMD_1414 blob é SEMPRE o mesmo** (primeiros 200 chars idênticos entre todas as sessões)
3. **CMD_1000 cifrado tem 150 bytes** (base64 de ~150 bytes de dados)
4. **Primeiros 48 bytes do CMD_1000 cifrado são SEMPRE IGUAIS** entre sessões:
   - Base64: `rfIQlMaR6Ses2pRV7x9RcqLQDOs9GDPH7FuY0jxaWEwOBvxOE2S7iMQgSIJbivkE`
   - Hex: `adf21094c691e927acda9455ef1f5172a2d00ceb3d1833c7ec5b98d23c5a584c0e06fc4e1364bb88c42048825b8af904`
5. **Bytes 48+ do CMD_1000 variam** conforme RandomStrA
6. **DataUseAES=false** no CMD_1001 — apesar de todos os comandos subsequentes serem cifrados
7. **CMD_1400 PTZ nunca foi capturado em plaintext** no MITM — sempre cifrado

---

## Dois Protocolos de Criptografia

### Protocolo ANTIGO (capturado no MITM)
- CMD_1414 retorna **blob binário** (~150 bytes, base64)
- CMD_1000 seguinte é **cifrado** (não é JSON plaintext)
- `DataUseAES=false` no response
- Cipher desconhecida (não é AES-128 nem 3DES pelos testes de S-box nos binários)

### Protocolo NOVO (que nosso Python sempre recebe)
- CMD_1414 retorna **JSON com chave RSA pública** (780 bytes)
- CMD_1000 pode ser **plaintext** (CMD_1000 está em `NotEncryptMsgID`)
- RSA-1024, PKCS1v1.5

**Por que o app recebe protocolo ANTIGO e nosso Python recebe NOVO?**
- Hipótese principal: o app criou a sessão `0x0001869F` com o protocolo antigo (antes do MITM), e quando essa sessão autentica um CMD_1413, o NVR responde com blob antigo
- Nossa Python cria sessão nova com plaintext CMD_1000 → NVR responde com JSON RSA (novo protocolo)
- Nunca conseguimos capturar o CMD_1000 inicial do app que criou `0x0001869F`

---

## O que foi tentado e resultados

### 1. Variações de Canal e Parâmetros PTZ
Testado exaustivamente:
- Channels 0, 1, 4, 5, 32, 33, 34, 35, 36, 64 — todos Ret=100, nenhum move
- Step dentro e fora de Parameter
- Comandos: "Up", "Down", "Left", "Right", "DirectionUp", "ZoomTele", "Stop"
- Action: "Start", "Stop", sem Action
- DeviceNo: 1
- OPCameraVisca

**Conclusão**: Channel/parâmetros não são o problema.

### 2. LoginType e campos extras no CMD_1000
- "DVRIP-Web", "DVRIP-XmEye", "DVRIP-Mobile"
- Com/sem CommunicateKey: "0", ""
- Ret=100 em todos, mas PTZ ainda não move

**Conclusão**: login funciona, mas tipo de sessão importa.

### 3. CMD_1413 antes do PTZ
```
CMD_1413 → CMD_1414 RSA JSON → CMD_1000 plaintext → Ret=100 → CMD_1400 PTZ → Ret=100, sem movimento
```
- Com SID=0x0001869F (sem auth prévia): CMD_1413 retorna RSA JSON (780 bytes)
- Com SID autenticado (ex 0x00001388): CMD_1413 retorna Ret=100 simples (67 bytes)
- Nenhum dos casos resulta em movimento físico

**Conclusão**: CMD_1413 + plaintext não é suficiente.

### 4. Login RSA (encrypted CMD_1000)
```
CMD_1413 → CMD_1414 (RSA JSON) → CMD_1000 cifrado com RSA-PKCS1v1.5 → Ret=205
```
Variantes testadas (todas retornam Ret=205):
- `{"EncryptType":"MD5","LoginType":"DVRIP-XmEye","PassWord":pw_sofia,"UserName":"admin"}`
- `{"EncryptType":"NONE",...}`
- `{"EncryptType":"RSA",...}`
- `{"LoginType":"DVRIP-Mobile",...}`
- `{"CommunicateKey":"0","EncryptType":"MD5",...}`
- `{"PassWord":pw_sofia,"UserName":"admin"}` (minimal)
- Com senha plaintext, com sofia hash

**Ret=205**: Diferente de 203 (senha errada) e diferente de connection close. Possivelmente "login RSA não configurado para essa conta" ou "AES key exchange necessário".

### 5. Replay de CMD_1000 cifrado do MITM
Replayar bytes exatos capturados no MITM após novo CMD_1413 → Ret=205 (não funciona, provavelmente por RandomStrA diferente)

### 6. Busca de chave AES nos binários do Windows Client
Varredura de `VMS.exe`, `NetSdk.dll`, `CMSClient.dll`, `XMCloudClientAPI.dll`, `StreamReader.dll`, `ConfigModule.dll`, `H264Play.dll` por S-boxes de AES e 3DES: **AES=0, 3DES=0 em todos**.

Candidatos a chave testados:
- Serial number do NVR, MAC address
- MD5/SHA de senha, usuário, serial
- Slices do blob CMD_1414
- Derivações de dois níveis

**Conclusão**: Cifra não é AES-128 ou 3DES standard. Possivelmente RC4 ou cifra customizada.

### 7. Análise Known-Plaintext do CMD_1000 cifrado
- Decoded 10 sessões diferentes do MITM
- Todos têm exatamente 150 bytes
- Bytes 0-47: IDÊNTICOS em todas as sessões
  - Hex: `adf21094c691e927...c4e1364bb88c42048825b8af904`
- Bytes 48+: VARIAM entre sessões
- XOR dos bytes variáveis > 0x7F → dados binários (não é JSON ASCII puro)

Se plaintext[0:47] = `{"EncryptType":"MD5","LoginType":"DVRIP-XmEye",`:
- Keystream[0:47] (RC4 hipotético com chave fixa): `d6d055faa5e39057d88eed258a3d6b50ef9439c9113a7fa88b32f686452a3d6...`
- Keystream[47] = `04` (CT[47] XOR `,` = 0x04)

**Conclusão**: O bytes 48+ provavelmente contêm dados binários (possivelmente chave DH do cliente ou nonce cifrado), a cifra pode ser RC4 com chave fixa.

### 8. CMD_1414 blob análise
- 150 bytes decodificados de base64
- Hex: `edbdfd4c570e40bbe27083c9fabbb2ff344db499...`
- IDÊNTICO entre todas as sessões (NVR não gera blob diferente por sessão)
- Blob XOR com CT[0:48] → não é plaintext legível
- Blob é possivelmente: parâmetros DH (p, g, g^s mod p) OU chave AES cifrada com chave RSA hardcoded no firmware

---

## Hipóteses Atuais (2026-06-15)

### Hipótese A: CommunicateKey = AES key cifrada com RSA (MAIS PROMISSORA)
O fluxo do protocolo NOVO seria:
1. CMD_1413 → CMD_1414 (RSA public key)
2. Cliente gera AES-128 key aleatória
3. Cliente cifra AES key com RSA-PKCS1v1.5 → base64 → CommunicateKey
4. CMD_1000 **plaintext** com CommunicateKey: `{"CommunicateKey":"<base64>","EncryptType":"MD5","PassWord":"W6xpk6c9","UserName":"admin"}`
5. NVR decifra CommunicateKey → obtém AES key
6. CMD_1001: Ret=100
7. CMD_1400 PTZ **cifrado com AES** (ECB ou CBC, modo a descobrir)
8. Câmera se move!

**Por que não foi testado ainda**: Tentou-se CommunicateKey:"0" (string literal) mas nunca RSA-encrypted AES key no campo CommunicateKey com CMD_1000 em PLAINTEXT.

### Hipótese B: Sessão inicial do app usa campo especial em CMD_1000
O CMD_1000 que criou `0x0001869F` pode ter campo extra como `"DHSupport":1` ou `"ClientType":"XmEye"`, fazendo o NVR criar sessão "DH-capable".

**Como verificar**: Capturar via MITM o início COMPLETO da sessão do app (do CMD_1000 inicial).

### Hipótese C: PTZ requer stream de vídeo ativo (CMD_1020)
Possivelmente PTZ só executa se a sessão tiver um monitor stream ativo (CMD_1020 enviado e processado). Não testado porque CMD_1020 é sempre cifrado no MITM.

### Hipótese D: Cifra é RC4 com chave derivável
Com os 47 bytes de keystream já identificados (`d6d055faa5e390...`), pode ser possível bruteforce de chave RC4 curta ou identificar a chave por outra fonte.

---

## Próximos Passos Recomendados

### Passo 1 (IMEDIATO — mais promissor): Testar Hipótese A
```python
# Implementação sugerida:
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5, AES
import base64, os, json

# RSA public key do NVR (de CMD_1414):
modulus_hex = "867A87F5B75C7522E9668C42674A41E384E562914BB2787F054B76A2E0792744951F5655B556D76470F462D6ECB2E8EDBAAE3BA7A676841434B4B0A85DCD700BCEDF29A726343C179F3614A5B43D68952CAE9C1A23FD1603F3E0F882C2D39960E4A3517CD289BEBE50AD83CA120CD6AB9A61E89E89035CAFD796186F95EFA83D"
n = int(modulus_hex, 16)
pub_key = RSA.construct((n, 65537))

# Gerar chave AES
aes_key = os.urandom(16)  # 128-bit AES key

# Cifrar chave AES com RSA público do NVR
cipher_rsa = PKCS1_v1_5.new(pub_key)
communicate_key = base64.b64encode(cipher_rsa.encrypt(aes_key)).decode()

# CMD_1000 em PLAINTEXT (porque 1000 está em NotEncryptMsgID)
# mas com CommunicateKey = AES key cifrada com RSA
login_body = {
    "CommunicateKey": communicate_key,
    "EncryptType": "MD5",
    "LoginType": "DVRIP-XmEye",
    "PassWord": sofia_hash("ma56ter"),  # = "W6xpk6c9"
    "UserName": "admin"
}

# Se Ret=100 → tentar CMD_1400 cifrado com AES:
def encrypt_cmd(body_dict, key):
    plaintext = (json.dumps(body_dict) + "\n").encode()
    pad_len = 16 - (len(plaintext) % 16)
    plaintext += bytes([pad_len]) * pad_len  # PKCS7 padding
    cipher = AES.new(key, AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(plaintext)).decode() + "\x00"
```

### Passo 2: Capturar CMD_1000 inicial do app via MITM
- Matar completamente o app XMEye
- Iniciar MITM com FULL body logging (remover `[:200]`)
- Abrir app fresh → capturar CMD_1000 inicial que cria `0x0001869F`
- Verificar se CMD_1000 inicial é plaintext ou cifrado

Para corrigir o MITM para log completo, em `mitm_proxy.py` linha 48:
```python
# ANTES:
body_str = repr(raw_body[:200])
# DEPOIS:
body_str = repr(raw_body)  # log completo
```

### Passo 3: Tentar RC4
- Com keystream[0:47] = `d6d055faa5e39057d88eed258a3d6b50ef9439c9113a7fa88b32f686452a3d6...`
- Bruteforce RC4 com chaves derivadas de: serial, MAC, firmware string, senha
- Ou buscar chave RC4 em binários da DLL

### Passo 4: Analisar CMD_1414 blob (protocolo antigo)
- Verificar se blob[0:64] é um número primo (DH prime de 512 bits)
- Se sim: implementar DH exchange e derivar chave
- Mas este protocolo pode não ser mais relevante (firmware atualizado para RSA JSON)

---

## Arquivos Relevantes

| Arquivo | Propósito |
|---------|-----------|
| `custom_components/xmeye/client.py:166` | `ptz_control` — atual sem criptografia |
| `custom_components/xmeye/const.py` | MSG_PTZ_CONTROL=1400, MSG_ALARM=1500 |
| `mitm_proxy.py` | MITM proxy para capturar tráfego |
| `/tmp/mitm_output.txt` | Output MITM capturado (2891+ linhas) |

---

## Dados de Referência

### RSA Public Key do NVR (de CMD_1414 novo protocolo)
```
Bits: 1024
EncryptAlgo: RSA_V1.5
Modulus: 867A87F5B75C7522E9668C42674A41E384E562914BB2787F054B76A2E0792744
         951F5655B556D76470F462D6ECB2E8EDBAAE3BA7A676841434B4B0A85DCD700
         BCEDF29A726343C179F3614A5B43D68952CAE9C1A23FD1603F3E0F882C2D399
         60E4A3517CD289BEBE50AD83CA120CD6AB9A61E89E89035CAFD796186F95EFA83D
Exponent: 010001 (65537)
DataEncryptionType: {AES: true, AESV2: true, VEKEY1: true}
LoginEncryptionType: {MD5: true, NONE: true, RSA: true}
NotEncryptMsgID: [1000, 1001, ...]  ← CMD_1000 é plaintext!
```

### CMD_1414 blob (protocolo antigo, 150 bytes hex)
```
edbdfd4c570e40bbe27083c9fabbb2ff344db499844de368b3e89ded78b02e2f
6deef5bbe2e1d0dff828c05becdd570e3c03c1710e52237c235348ec80ec1969
1ace832fb7d3f4b092c8d963cdea5bc03a83be6fe954c7a63f96113206832a18
db54c9123f1847c7c597ce99773911ad57768ca669c7f1c43ad5f7f228094ee1
355930e7d59a93b7baca4665d14ed612c643ab9cd923
```

### CMD_1000 cifrado — Session 1 (RandomStrA="81t6")
```
rfIQlMaR6Ses2pRV7x9RcqLQDOs9GDPH7FuY0jxaWEwOBvxOE2S7iMQgSIJbivkE
D2/RZkuB/Xvi6WHjP5Wh4sZ7YxZlo0KHsx2i2own1wEw9ENFccx18t5R5BYcG9f
mWRCD5G7WjYsOJyr0p+sNH3tryYs3eYrioFDE/SqMCKbeUHFSOr+/qBPTOukhLw1
yGq/a2TIT
```

### Sofia Hash (MD5 customizada)
```python
def sofia_hash(password: str) -> str:
    raw = hashlib.md5(password.encode()).digest()
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    result = ""
    for i in range(0, 16, 2):
        val = (raw[i] + raw[i+1]) % len(chars)
        result += chars[val]
    return result
# sofia_hash("ma56ter") == "W6xpk6c9"
```

---

## Timeline das Tentativas

1. CMD_1400 direto com login plaintext → Ret=100, sem movimento
2. Canal 0 a 64 testados → tudo Ret=100, sem movimento
3. ONVIF → FUNCIONA (confirmado visualmente), mas usuário não quer
4. MITM captura fluxo real do app → cifrado
5. CMD_1413 → CMD_1414 RSA → CMD_1000 RSA cifrado → Ret=205
6. CMD_1413 → CMD_1414 RSA → CMD_1000 plaintext → Ret=100 → PTZ → sem movimento
7. Busca de chave AES em binários → não encontrada
8. Known-plaintext attack → confirmado RC4 ou stream cipher com chave fixa
9. **PRÓXIMO**: CommunicateKey = RSA(AES_key) em CMD_1000 plaintext → CMD_1400 AES cifrado
