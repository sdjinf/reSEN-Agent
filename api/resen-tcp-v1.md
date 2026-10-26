---
name: reSEN TCP 加密协议 v1
description: reSEN-Agent 纯 TCP 传输加密协议 v1：帧结构 [len][counter+fcode][AEAD密文+tag]、HKDF 方向绑定密钥树、hello 握手、HMAC 单调 counter、持久水位与熔断，逐段与 api/connect 实现对应。
tags: [resen, protocol, tcp, crypto, aead, hkdf]
created: 2026-10-21 15:52:17
updated: 2026-10-26 00:00:00
---

# reSEN-Agent TCP 加密协议 v1

> 状态：**已落地**。传输层在 `api/connect/`（`pack` / `crypto` / `chacha` / `counter` / `session`），
> 业务层在 [`peer_api.py`](peer_api.py)（`chat()` 发、`serve()` 收）。
> 唯一的机密是预共享 `access_key`——**全部网络攻击面收敛为“密钥保存”这一个问题**。

---

## 0. 一句话结论

```
帧 = [len 4B][counter 8B][fcode 8B][AEAD(明文+tag) ...]
                      └─ fresh 段 16B ─┘
```

- `fcode = HMAC-SHA256(seed_dir, counter_be64)[:8]`：`counter` 明文可见，`fcode` 是密钥派生的校验值。
- 加密用 AEAD（AES-256-GCM 或 ChaCha20-Poly1305），`AAD = len || counter || fcode`。
- 密钥由 `access_key` + 双方机器特征 `fp` 经 HKDF-SHA256 派生，收发方向分离。
- `counter` 严格单调、跨重连/重启持久递增 → 防重放；HMAC 不可逆 → 防预测与反推种子。

---

## 1. 安全模型

| 项 | 内容 |
|---|---|
| 唯一机密 | `access_key`（带外预共享） |
| 公开可见（不视为秘密） | 帧长度、`counter`、`fcode`、`fp`（机器特征） |
| 敌手能力 | 监听、记录、重放、篡改、主动注入、重连 |
| 敌手不知 | `access_key` |
| 达成目标 | 机密性、完整性、防重放、防反射（回射帧方向不对，必然验不过） |

设计哲学：**`fp` 只是域分离与“连接一致性”校验，不是安全来源**。即使中间人完全知道双方 `fp`，只要没有 `access_key`，也算不出任何有效 `fcode`，更解不开任何密文。

---

## 2. 帧结构（字节级）

```
+--------+-------------+------------------------------+
| len 4B | fresh 16B   | ciphertext + tag 16B         |
+--------+-------------+------------------------------+
          | counter 8B | fcode 8B |
```

- `len`（4B，网络序 uint32）= `FRESH_LEN + len(ciphertext)` = `16 + 1 + len(data) + 16`，即 **fresh 段 + 加密段** 的总长，**不含自身 4B**。
- `counter`（8B，网络序 uint64）：发送方单调递增计数，**明文可见**。
- `fcode`（8B）：`HMAC-SHA256(seed_dir, counter)` 取前 8 字节，**常数时间比对**。
- 加密段 = AEAD 密文（含 16B tag），密文对应明文 `type(1B) || data`。

> `type`（1B）放在**加密段内**，避免任何明文可探测字段。v1 帧类型：`0x01=data`、`0x02=hello`、`0x03=ping`、`0x04=bye`。

---

## 3. 各段是怎么算出来的

### 3.1 `fp`（机器特征）
```
fp = SHA256( "reSEN-fp-v1" || machine_id || platform.node() || platform.system() || install_uuid )[:16]
```
- `machine_id`：Linux 读 `/etc/machine-id`（回退 `/var/lib/dbus/machine-id`）、Windows 读注册表 `MachineGuid`，
  都拿不到时用 `uuid.getnode()`。
- `install_uuid` 存在 `configure/peer_identity.json`（`{"install_uuid": ..., "created": ...}`），首次使用时生成，
  用来隔离**同一台机器上的多实例**（否则同一 `access_key` 的两个进程会撞 `counter` 空间）。
- 稳定、可复现；**即使变化也不泄露机密**，只影响连接可用性。进程内由 `local_fingerprint()` 缓存一份。

### 3.2 密钥派生树（HKDF-SHA256）

```
K_psk  = HKDF-Extract(salt="reSEN-v1", IKM=access_key)          # 32B
K_tx   = HKDF-Expand(K_psk, info="key"   || fp_local  || fp_remote, 32)
K_rx   = HKDF-Expand(K_psk, info="key"   || fp_remote || fp_local,  32)
seed_tx= HKDF-Expand(K_psk, info="fcode" || fp_local  || fp_remote, 32)
seed_rx= HKDF-Expand(K_psk, info="fcode" || fp_remote || fp_local,  32)
```
- `info` 只有两种：`b"key" + fp_local + fp_remote` 与 `b"fcode" + fp_local + fp_remote`。
- **方向分离**：方向只由 `fp` 顺序表达——`A.tx` 与 `B.rx` 落到同一个 info，因此密钥一致；把收到的帧回射回去时方向对不上，`fcode` 与 tag 必然验不过。
- `fp` 顺序天然表达“谁的本机特征在前”，因此两端完全对等（服务端作为客户端也走同一公式）。
- HKDF 用 `hmac` + `hashlib` 直接实现（`hkdf_extract` / `hkdf_expand`）：Extract 一次，Expand 按需多块。

### 3.2.1 hello（交换 `fp` 与算法）

方向密钥依赖对端 `fp`，而第一帧之前还不知道它，所以握手用一把与方向无关的固定密钥：

```
K_hello    = HKDF-Expand(K_psk, info="hello",       32)
seed_hello = HKDF-Expand(K_psk, info="fcode-hello", 32)
```

- `Channel.open()` 一拿到 TCP 流就发一帧 `HELLO`，载荷 17B：`fp(16) || algo(1)`；
  加密固定用 ChaCha20-Poly1305（内置纯 Python 实现，不依赖 `cryptography`）。
- `algo(1)` 是本机首选算法号：`1 = aes-256-gcm`、`2 = chacha20-poly1305`。
- 首次 `send()` / `recv()` 之前会 `_ensure_ready()`：读对端 `HELLO` → 记下 `remote_fp` → 派生方向密钥 →
  `negotiate_algo()`（双方都是 `1` 才用 AES-256-GCM，否则 ChaCha20-Poly1305）。
- hello 帧的 `counter` 也从发送 counter 空间取号，`fcode` / tag 照常校验，但**不推进 `last_rx`**
  （`check_replay=False`）；`recv()` 遇到迟到的 `FT_HELLO` 直接跳过。
- 首帧不是 `FT_HELLO` 或载荷不是 17B → `ProtocolError`；如果调用方用 `remote_fp` 钉扎了指纹而对不上 → `AuthError`。
- 双方 `HELLO` 都是建连即发，所以业务帧不必等对端 hello 回来就能写出去。

### 3.3 `counter`（发送计数 + 接收水位）

单调性由三件事共同保证（[`counter.py`](connect/counter.py)）：

1. **预留式落盘**：内存里每帧自增；取号后若 `c + 1 >= saved_next`，就把 `saved_next` 推到
   `c + 1 + COUNTER_RESERVE`（默认 1024）并立刻 `commit()`，所以文件里的值永远跑在已用编号前面。
   崩溃后从 `saved_next` 起跳，**绝不重号**。
2. **时钟兜底**：每次取号都算
   ```
   c = max( 内存下一号 , saved_next , now_ms )
   ```
   即使状态文件被回滚（从备份恢复/被删）或进程重启，真实时间仍在前进，`counter` 依旧 > 历史值。
3. **接收端只要求 `counter > last_rx`**（不是 `== last_rx+1`）。因此**丢包、断线重连都不会造成错位死锁**。

- 发送计数按**本机 `fp`** 只记一个：同一安装的所有出站连接共用一个编号空间，不会撞号。
- 接收水位按**对端 `fp`** 各记一个：同一台对端重连后仍接着比大小。
- `last_rx` 每接受 `COMMIT_EVERY`（默认 256）帧落盘一次；正常关闭时 `close()` 会 `flush()`。

### 3.4 `fcode`
```
fcode = HMAC-SHA256(seed_dir, counter_be64)[:8]
```
- 8B = 64-bit，随机猜中概率 2⁻⁶⁴/次（`FCODE_LEN` 固定 8）。
- 只取前若干字节的 HMAC：**不可逆（无法由 fcode 反推 seed）、不可预测（不知道 seed 就算不出 counter+1 的 fcode）**。
- 中间人即使录下整条 `counter/fcode` 序列，也只能重放旧帧 → 被 `counter` 单调性拒绝；想造新帧就得算 HMAC → 没有 `access_key` 做不到。

### 3.5 `nonce`
```
nonce = 0x00000000 (4B 零占位) || counter (8B)   # 96-bit nonce（GCM / ChaCha 通用）
```
- 直接用 `counter`，无需在帧里额外塞 nonce（省 12B）。
- 因为 `nonce` 只在本方向的 `K_dir` 下使用，且 `counter` 严格单调 → 不会 nonce 复用。

### 3.6 AAD 与 tag
```
AAD  = len(4B) || counter(8B) || fcode(8B)          # 20B
tag  = AEAD(K_dir, nonce, 明文, AAD) 的 16B 认证标签
```
- 长度字段与 fresh 段被**认证**，改一个 bit 即解密失败 → 消除长度/Length-Extension 类攻击面。

### 3.7 明文
```
明文 = type(1B) || data
data 可为空（如 ping）。
```

---

## 4. 收发流程

### 发送（`Channel.send(type, data)`）
1. `_ensure_ready()`：等对端 hello，拿到 `remote_fp` 后派生 `Keys`、协商算法
2. `c = take_tx(key)`（自增 + 预留式落盘，见 3.3）
3. `f = freshcode(seed_tx, c)`
4. `plaintext = type || data`；`len = FRESH_LEN + 1 + len(data) + TAG_LEN`
5. `ct = seal(algo, K_tx, nonce(c), plaintext, AAD = len || c || f)`
6. 写 `[len][c][f][ct]`，然后 `drain()`

### 接收（`Channel.recv()`）
1. 读 4B `len`；不在 `MIN_LEN..MAX_LEN` 内 → `FrameError`（抛给调用方关连接）
2. 读满 `len` 字节，`datapack.unpack()` 做结构校验（失败包成 `ProtocolError`）
3. `accept_rx(key, c)`：`c > last_rx` 才接受，接受时**立即把 `last_rx` 推到 `c`**；否则 `ReplayError`
4. `f' = freshcode(seed_rx, c)`，`hmac.compare_digest` 比对；不等 → `AuthError`
5. `open_(algo, K_rx, nonce(c), ct, AAD = len || c || f)`；tag 失败 → `AuthError`
6. 明文为空 → `FrameError`
7. `FT_HELLO` 跳过，其余返回 `(type, data)`

> `last_rx` 的推进在第 3 步、早于 `fcode`/tag 校验（先判“新不新”、再验“真不真”）：
> 认证失败帧也会占掉水位，只要下一帧 `counter` 更大就照常通过，不会因失败而卡死；
> 代价是能往链路里插包的人可以用超大 `counter` 把水位推高（只影响可用性，仍是断连/丢帧级别）。

### 拒绝与熔断

| 条件 | 异常 | 处置 |
|---|---|---|
| `len` 越界 / 明文为空 | `FrameError` | 抛出，连接关闭 |
| 帧结构不合法（`unpack`） | `ProtocolError` | 抛出，连接关闭 |
| 首帧不是 `FT_HELLO` / 载荷不是 17B | `ProtocolError` | 抛出，连接关闭 |
| `c ≤ last_rx` | `ReplayError` | 丢帧 + 失败数 +1 |
| `fcode` 不匹配 | `AuthError`（密钥错 / `fp` 已变 / 伪造） | 丢帧 + 失败数 +1 |
| tag 失败 | `AuthError`（篡改） | 丢帧 + 失败数 +1 |
| 失败数 ≥ `MAX_FAILS`（3） | `ProtocolError` | `abort()` 断开 |
| 任一帧通过 | — | 失败数清零 |

> 认证/重放失败是“丢帧继续读”，凑满 3 次才熔断，熔断后不自动重连。
> `session.backoff_delay(fails)` 给出 `5s × 2^(n-1)`、封顶 300s 的退避建议值，供上层决定何时重试。

`fp` 变化后的表现：两边派生出不同的 `K_tx/K_rx/seed_*` → 首帧即 `fcode` 不匹配 → `AuthError`。
`fp` 未变时断开重连不受影响（`counter` 按 `fp` 持久，不随连接重置）。

---

## 5. 代码接口（[`api/connect`](connect/readme.md)）

| 模块 | 公开内容 |
|---|---|
| [`pack.py`](connect/pack.py) | `datapack`（`build`/`topack`/`unpack`）、`freshcode`、`aad`、`frame_len`、`ciphertext_len`、长度常量、`FrameError` |
| [`crypto.py`](connect/crypto.py) | `machine_id`、`install_uuid`、`fingerprint`、`psk`/`derive`/`Keys`、`hello_key`、`nonce`、`seal`/`open_`、`available_algo`/`negotiate_algo`/`algo_name` |
| [`chacha.py`](connect/chacha.py) | 纯 Python ChaCha20-Poly1305（RFC 8439），无第三方依赖 |
| [`counter.py`](connect/counter.py) | `CounterState`（`take_tx`/`accept_rx`/`rx_last`/`commit`/`flush`）、`shared_state()`、`COUNTER_RESERVE`/`COMMIT_EVERY` |
| [`session.py`](connect/session.py) | `Channel`、`connect`、`request`、`serve`、`PeerServer`、`local_fingerprint`、`backoff_delay`、帧类型/`PORT`/`MAX_FAILS`、四个异常类 |

```python
from api.connect import session

# 一问一答：建连 → hello → 一帧 DATA → 等 DATA → 关闭
reply = await session.request("10.0.0.2", 1124, "my-key", b'{"messages": []}')

# 长连接（remote_fp 可选，用于钉扎对端指纹）
async with await session.connect("10.0.0.2", 1124, "my-key", remote_fp="<32 hex>") as ch:
    await ch.send(session.FT_DATA, b"hello")
    ftype, data = await ch.recv()
    await ch.ping()

# 服务端：handler 可同步可异步，返回 bytes 即回一帧 DATA
async def handler(data: bytes) -> bytes:
    return data.upper()

server = await session.serve(handler, "0.0.0.0", 1124, "my-key")
```

- `request()` 只等第一个 `FT_DATA`；途中的 `FT_HELLO`/`FT_PING` 忽略，收到 `FT_BYE` 抛 `ClosedError`。
- `serve()` 收到 `FT_PING` 原样回一帧 `FT_PING`，收到 `FT_BYE` 收工；连接异常只记日志，不影响其他连接。
- `PeerServer` 是同步包装：构造即绑端口（被占立刻 `OSError`），`serve_forever()` 阻塞，`shutdown()`/`server_close()` 收尾。
- 自检：`python -m api.connect.selftest`（RFC 8439 向量、帧布局、密钥对称性、回环、重放/篡改/1MB 边界）。

---

## 6. 常量与状态文件

| 名称 | 值 | 出处 / 说明 |
|---|---|---|
| `MAX_FRAME` | 1,024,000 | 整帧上限（含 4B 长度字段） |
| `HDR_LEN` / `LEN_FMT` | 4 / `">I"` | 网络序 uint32 长度字段 |
| `FRESH_LEN` | 16 | `counter 8` + `fcode 8` |
| `FCODE_LEN` | 8 | HMAC-SHA256 截断 |
| `TAG_LEN` | 16 | AEAD 认证标签 |
| `MIN_LEN` | 33 | 空 `data` 时的 `len`（16+1+16） |
| `MAX_LEN` | 1,023,996 | `MAX_FRAME - HDR_LEN` |
| `MAX_CT` | 1,023,980 | `MAX_LEN - FRESH_LEN` |
| `MAX_DATA` | 1,023,963 | `MAX_CT - TAG_LEN - 1` |
| `FP_LEN` / `KEY_LEN` | 16 / 32 | `fp` 长度、HKDF 输出长度 |
| `SALT` / `FP_PREFIX` | `b"reSEN-v1"` / `b"reSEN-fp-v1"` | HKDF salt、`fp` 域前缀 |
| `ALGO_AES_GCM` / `ALGO_CHACHA20_POLY1305` | 1 / 2 | hello 载荷里的算法号 |
| `HELLO_ALGO` | 2 | hello 固定用内置 ChaCha20-Poly1305 |
| `PORT` | 1124 | 默认端口 |
| `MAX_FAILS` | 3 | 连续失败熔断阈值 |
| `COUNTER_RESERVE` / `COMMIT_EVERY` | 1024 / 256 | 发送预留窗口、接收水位落盘间隔 |
| 帧类型 | `FT_DATA=1`、`FT_HELLO=2`、`FT_PING=3`、`FT_BYE=4` | `session.py` |

状态文件（都在 `configure/`，被 `.gitignore` 的 `*/*.json` 覆盖）：

| 文件 | 内容 |
|---|---|
| `peer_identity.json` | `{"install_uuid": "<32 hex>", "created": <时间戳>}`，`fp` 的实例隔离项 |
| `peer/<install_uuid>.json` | `{"tx": {"<本机 fp hex>": saved_next}, "rx": {"<对端 fp hex>": last_rx}}` |

---

## 7. 性质与边界

- **无证书链**：双方共享 `access_key` 即可建连，不需要 CA、域名或证书轮换。
- **握手只有一帧**：hello 建连即发，业务帧不必等对端 hello 回来才写；两边各发一帧即完成 `fp` 交换与算法协商。
- **明文面只有 `len` / `counter` / `fcode`**（外加时序）：`type` 在密文内，端口扫不出协议特征。
- **可选身份钉扎**：`connect(remote_fp=...)` 把对端 `fp` 钉住，指纹变动直接 `AuthError`。
- **可选 AES 加速**：装了 `cryptography` 且双方都支持才协商 AES-256-GCM，否则一律内置 ChaCha20-Poly1305。
- **风险边界**：全部收敛在 `access_key` 的保存上；`fp` 只是域分离 / 一致性校验，泄露 `fp` 不算失密。
