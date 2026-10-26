# api
> `peer_api` 是 peer 协议的**两端**：收（服务端）+ 发（`peer_agent` 工具用的客户端）。
> 传输层是裸 TCP：所有载荷都装进 [`api/connect`](connect/readme.md) 的 reSEN TCP v1 加密帧里，**不是 HTTP**。

与 [agentapi](../agentapi/readme.md) 的分工：
- `agentapi` 是本地 agent 核心（OpenAI 兼容接口 + 元工具/热插拔工具）
- `api` 只负责网络：[`serve()`](#接收端服务端) 收别人发来的对话交给本地 agent 答，[`chat()`](#await-chatusrmsg-access_keyno_access_key-urlno_apiurl-timeout600-clientnone---ccmp) 把对话发给别的 peer

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 传输 | [`connect/`](connect/readme.md) | 帧格式、`fp`/HKDF 密钥派生、counter 防重放、hello 握手、熔断 |
| 业务 | [`peer_api.py`](peer_api.py) | `messages` JSON、调用本地模型、执行工具、设备清单 |

> 唯一机密是预共享 `access_key`：它直接当会话密钥用，**不随包传输**。hello / `fcode` / AEAD tag
> 过不去的连接根本进不来，所以没有 HTTP 时代那层“403 鉴权”。

## [peer_api.py](peer_api.py)

### `await chat(usrmsg, access_key=NO_ACCESS_KEY, url=NO_APIURL, timeout=60.0, client=None) -> ccmp`
- **usrmsg**：上下文，`list[ccmp]`，序列化成 `{"messages": [...]}` 放进一个 DATA 帧
- **access_key**：会话密钥（同时用于派生 `K_tx`/`K_rx`），默认占位值 `"NO-ACCESS_KEY"`；**还是占位值就直接报错**
  （“不想加密”要显式传空串 `""`）
- **url**：远端端点，由调用方传入（默认占位值 `"NO-APIURL"`，没配就报错）。写法 `host` / `host:port` / `tcp://host:port`，
  缺协议前缀当 `tcp://`，**没写端口就补 `PORT`（1124）**
- **timeout**：单次请求超时（秒，默认 60）；建连另有 10s 上限
- **client**：仅为兼容旧 HTTP 版签名而保留，传了也会被忽略

```python
from api.peer_api import chat

reply = await chat(
    [{"role": "user", "content": "你好"}],
    access_key="my-key",
    url="192.168.1.7",        # -> tcp://192.168.1.7:1124
)
print(reply)   # {"role": "assistant", "content": "..."}
```

> 路径/查询串不参与通信：`chat()` 只取 `host` 和 `port`（`tcp://host:1124/anything` 也能用）。
> [`peer_agent`](../agentapi/meta_tools/peer_agent.py) 元工具则**主动拒绝**带路径的 url，免得误以为在调 HTTP。

### `with_port(url) -> str`
把 url 里缺的端口补成 `PORT`（缺协议前缀时补 `tcp://`），显式写了端口就原样返回；没有主机名则抛 `api_error`。

### 端口与地址
- `PORT = 1124`：peer 默认端口（来自 `api.connect.session.PORT`），客户端 `chat()` 内部对 `url` 走 `with_port()` 补齐
- `DEFAULT_HOST = "0.0.0.0"`：服务端默认监听地址
- 服务端实际监听地址取 `config.json` 的 `peer_host` / `peer_port`，缺失或非法时回落 `0.0.0.0:1124`（见 `configured_address()`，返回 `(host, port)`）

## 接收端（服务端）

### `serve(host=None, port=None, key=None, use_tools=None)`
起服务并阻塞（Cmd-C/Ctrl-C 停）；`host`/`port` 省略时取 `config.json` 的 `peer_host`/`peer_port`。命令行入口：
```bash
python -m api.peer_api                                   # 监听 config.json 的 peer_host/peer_port（缺省 0.0.0.0:1124）
python -m api.peer_api --host 127.0.0.1 --port 9000 --key my-key
python -m api.peer_api --no-tools                        # 强制关掉“远端驱动本地工具”（默认看 peer_tools）
python -m api.peer_api --probe tcp://host:1124 --key k   # 只打一发请求看结果（诊断用，不起服务）
```
- 启动前先校验：`access_key` 还是占位值、或 `config.json` 的 `apikey` 为空 → 抛 `api_error`，CLI 退码 1
- 读 `configure/config.json` 的 `apikey`/`baseurl`/`modelname` 跑**本地模型**；`access_key` 就是会话密钥，
  握手或 `fcode`/tag 校验不过的连接直接断（不区分“没带 key”和“key 错”）
- 每收到一个 DATA 帧：把里面的 `messages` 交给本地模型跑一轮（**use_tools 为真时工具就地执行**，最多
  `_MAX_ROUNDS = 8` 轮），把最后那条 ccmp 序列化成 JSON 回一个 DATA 帧
- **每次请求都是独立的**：用请求自带的那份对话答一轮，不碰本地 memory；要连续对话就由调用方把整段 `messages` 带上
- 请求体不是合法 JSON / 不是 object / `messages` 不是非空 `list[dict]` → 回 `{"error": "..."}`；本地模型出错同样回 `{"error": ...}`
  （客户端 `chat()` 会把它还原成 `api_error`）
- **处理时无论出什么事都回包**（连 `KeyboardInterrupt`/`CancelledError` 都兜住，并把完整堆栈写到日志）：
  否则连接会被默默关掉，客户端只能看到 `ClosedError: connection lost` 或超时，极难定位

### `probe(url, key=None, text="ping") -> int`
按 peer 协议打**一发**请求（`--probe` 就是它）：打印目标 `host:port`、key 是否提供、`content`，
以及回复的 `role`/`content`（有 `tool_calls` 只报条数）。退码：成功 `0`，出异常 `1`。
`key` 省略时取 `config.json` 的 `access_key`；连不上就直接打印 `✗ 异常类型: 信息`，不会挂住。

### 错误 → 先查什么
`chat()` / `probe()` 把所有异常统一包成 `api_error`（原始异常挂在 `__cause__`），消息形如 `异常类型: 信息（请求 host:port）`。

| 报错里出现 | 含义 | 先查 |
|---|---|---|
| `access_key 还是占位值` | 客户端自己拦下的，**没发请求** | 传真 key；“用空密钥”要显式传 `""` |
| `没有可用的 url` | 没传 url / 还是 `NO-APIURL` | 传 `host[:port]` |
| `usrmsg 是空的` | 没给消息 | 至少一条 ccmp |
| `ConnectionRefusedError` / `OSError: Connect call failed` | host:port 上没人接受连接 | 对端服务起没起、端口对不对 |
| `gaierror` / `Name or service not known` | 域名解析不了 | 主机名 / DNS |
| `TimeoutError` | 建连或等回复超时 | 防火墙/网段、对端是否卡在模型调用 |
| `AuthError: fcode mismatch (wrong access_key or fingerprint)` | 两端 `access_key` 不一致，或某台机器 `fp` 变了 | 核对 key；`configure/peer_identity.json` 有没有被改 |
| `AuthError: AEAD authentication failed` / `malformed hello` | 密文被篡改，或端口上压根不是本项目 | 对端到底是谁（用 `--probe` 打一发） |
| `ReplayError: counter ... <= last_rx` | counter 回退（状态文件被回滚/复制） | `configure/peer/<install_uuid>.json` |
| `ClosedError: peer closed before reply` | 连上了但对端没回 DATA 帧就断 | **远端服务日志**（回包前的异常已兜底，多半是进程被杀） |
| `响应不是合法 JSON` / `响应不是一条 ccmp` | 对端不是 `peer_api` 服务，或版本不匹配 | 对端代码与端口 |
| `api_error: {error 文本}` | 对端业务层报错被还原 | 文本本身（模型/工具/请求体） |

### `make_server(key=None, use_tools=None, host=None, port=None) -> PeerServer`
只搭好不启动：构造时**就绑定端口**（被占则立刻 `OSError`），起停自己控制
（`srv.serve_forever()` / `srv.shutdown()`，也能嵌到别的进程里）。
`host`/`port` 传了就用传的，否则取 `config.json` 的 `peer_host`/`peer_port`；`key` 不传取 `config.json` 的 `access_key`。
`PeerServer` 另带 `api`（本地 `agent_api`）与 `use_tools` 属性，`srv.host`/`srv.port` 是真实监听地址。

> ⚠️ 默认监听 `0.0.0.0`（可在 config.json 里用 `peer_host`/`peer_port` 改），而“远端能不能驱动本地工具”由 config.json 里的开关键决定：
> `"peer_tools": true | false`（缺省 **true**）。true 时拿到 `access_key` 的一方可以让本地模型
> 在你机器上执行工具，只在你信任的网络里跑；CLI 的 `--no-tools` 可以无视配置强制关。
> 这个键在 f2 的设置面板里能直接改（`core.settings()` 列了它），**改完要重启 TUI/服务才生效**（启动时读一次）。

> [TUI](../tui/main.py)（`python tui/main.py`）启动时会自动调 `configured_address()` + `make_server()` 起一份监听
> `config.json` 的 `peer_host:peer_port`（缺省 `0.0.0.0:1124`）的服务，退出时关掉；
> `access_key` 还是占位值（`api_error`）或端口被占（`OSError`）就只在聊天区提一句、不影响用 TUI。
> （所以手动跑 CLI 起服务时，记得 TUI 那边已经把配置的端口占了）
>
> 另有一个开关键 `"peer_serve": true | false`（缺省 **true**）决定 TUI 启动时起不起这份服务；
> 在 `^p` 的 **peer 栏**里可以当场开关服务（状态写在行里，回车就切，端口即时释放/占用），
> 而 `peer_serve`（下次启动生效）只在 `f2` 设置表格里改。两者都不影响本机自己聊天。

## 已连接过的设备（[`connect.json`](../configure/connect.json)）

`peer_agent` 每连上一台远端，就按下面这个格式记一笔；同一台（`host:port` 相同）只留一条。

```json
[
    {"name": "看板机", "desc": "车间的显示器", "host": "10.10.10.100", "port": "9999", "tag": ["lab"]}
]
```

- `name`/`desc`/`tag` 是给人看的：工具调用可以带上（`name`/`desc`/`tag` 参数），不传就只填 `host`/`port`，
  `name` 默认用主机名；重连时传了的字段会刷新、`tag` 取并集
- 地址里的端口走和 `chat()` 一样的默认端口逻辑（没写就是 [`PORT`](#端口与地址)）
- 文件不存在/空/写坏了都当成空清单，不拦工具调用；清单里全是空的那条骨架（手写模板）会被就地填掉

```python
await chat(...)                                  # 真正发请求
peer_api.devices()                               # -> [{name, desc, host, port, tag}, ...]
peer_api.remember(url, name="客厅机", tag=["home"])  # -> (是不是新条目, 该条目)
```

### 协议
- 传输：TCP + reSEN TCP v1 加密帧（见 [connect/readme.md](connect/readme.md)）；每次 `chat()` 建一条连接，一问一答
- 请求 DATA 帧：UTF-8 JSON `{"messages": [...]}`（**不再有** `access_key` 字段）
- 响应 DATA 帧：一条 ccmp；也接受套了一层的 `{"message": {...}}`；`{"error": "..."}` 会被还原成 `api_error`

### 错误
全部抛 `api_error`（原始异常挂在 `__cause__`），覆盖：url/access_key 是占位值、`usrmsg` 为空、url 没有主机名、
连不上/超时/协议错（`AuthError`/`ReplayError`/`ClosedError`/`FrameError`）、对端回 `{"error": ...}`、
响应不是合法 JSON、响应不是一条 ccmp。

## 谁在用
- 发：[元工具 peer_agent](../agentapi/meta_tools/peer_agent.py) —— 把一份 `list[ccmp]` 发给远端 peer agent 协作，
  `url` 取 `host[:port]` / `tcp://host[:port]`（默认 1124，带路径直接拒绝），连上后把对方记进 `connect.json`
- 收：远端 peer 的 `peer_agent` 工具打到本机 `peer_api.serve()` 上，本地模型代你应答
- 宿主：[TUI](../tui/main.py) 启动时按 `peer_serve` 配置起/停这份服务（`^p` 的 peer 栏可当场开关）
