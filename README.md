# reSEN-Agent 一个可以自己升级的Agent
> 该项目部分代码由deepseek-flash生成，所有代码均采用MIT协议开源
> 感谢textual和openai的开源代码，这为我节省了不少时间

## 怎么启动？How to Start?
最推荐的方案如下\
0. 环境（推荐环境，该教程以推荐环境为基准而编写的）：
  - [Python](https://www.python.org/downloads/) 3.14及以上版本
    - 该项目基于Python 3.14开发的，最低测试过的版本为3.14，版本跨度太大可能造成未知bug
  - [Git](https://git-scm.com/install/)
  - Windows / Linux / MacOS
    - 没有bash则无法使用一键脚本
1. 你需要一台可联网的设备用于获取项目源代码和依赖项
  - 手动启动
    1. 获取代码
    ``` bash
    git clone https://github.com/sdjinf/reSEN_Agent.git
    cd reSEN_Agent
    ```
    2. 创建虚拟环境
    ``` bash
    # Linux / MacOS
    python -m venv .venv
    source .venv/bin/activate
    ```
    ``` powershell
    # Windows(PowerShell)
    python -m venv .venv
    .venv/Scrpits/activate.ps1
    ```
    ``` batch
    rem Windows(cmd)
    python -m venv .venv
    .venv/Scrpits/activate.bat
    ```
    3. 安装依赖
    ``` bash
    python -m pip install --upgrade pip # 有时候pip版本过低会导致报错，建议升级
    pip install -r requirement
    ```
    4. 运行
      - 用户界面(TUI)
      ``` bash
      # Linux / MacOS
      python tui/main.py
      ```
      ``` powershell
      # Windows
      python .\tui\main.py
      ```
      - peer服务
      ``` bash
      # Linux / MacOS
      python api/peer_api.py
      ```
      ```powershell
      # Windows
      python .\api\peer_api.py
      ```
    5. 首次启动
      - 首次启动需要设置apikey和accesskey才能开始使用功能
      - 在用户界面中按下`f2`快速切换到可视化配置界面
  - 自动启动
    1. Linux/MacOS
      - 在终端(Terminal)中输入
      ``` bash
      sh start.sh
      ```
      或
      ``` bash
      bash start.sh
      ```
    2. Windows10 以下
      - 双击`start.bat`
    3. Windows10 及以上
      - 右键`start.ps1`，并点击`在PowerShell中运行`
      - 双击`start.bat`
2. 更新
  ``` bash
  git pull
  ```
3. 卸载
  - 删除reSEN_Agent文件夹即可

## 作为用户，我需要明白什么？As a User, What do I Need to Know?
1. 用户需要知道以下文件(目录)的用处
  - \_\_pycache\_\_
    - python是一个脚本语言不错，但是为了提高代码运行效率依然采用了编译方案。它的主要作用是通过缓存字节码来加速模块的导入过程，从而提升程序的运行效率。
    - 你可以删除`__pycache__`文件夹及其中的文件，但是你每次启动`reSEN-Agent`，这些文件夹和文件都会重新生成
  - [tui/](./tui)
    >tui包是用户界面基础，该包详细编写和定义了用户的使用界面
    - [main.py](./tui/main.py)
      - `main.py`是用户使用的入口，可以看作程序的起点，一切的一切都从此开始
  - [history](./history/)
    >history用于存放记忆
    - [long](./history/long/) 
      - 长期记忆，永久存储
    - [short](./history/short/)
      - 短期记忆，随用随删
  - [configure/](./configure/)
    >configure文件夹用于存储配置文件
    - [config.json](./configure/config.json)
      - 基础配置文件，该文件的所有配置均可在f2界面内修改
      - 配置语义(参考值与默认值不同)

        | 键 | 说明 | 参考值 |
        | --- | --- | --- |
        | `apikey` | OpenAI 兼容服务的 API Key | NO-API_KEY |
        | `baseurl` | 服务地址 | https://api.deepseek.com/ |
        | `modelname` | 默认模型名，TUI 中可点击切换 | deepseek-flash |
        | `access_key` | peer 服务鉴权密钥；设为空字符串表示不鉴权，保持 `NO-ACCESS_KEY` 则拒绝启动 peer | NO-ACCESS_KEY |
        | `reasoning_effort` | 推理强度：`minimal` / `low` / `medium` / `high` / `xhigh` / `max` | minimal |
        | `context_limit` | 上下文上限（token），用于底部用量显示，填写超过服务提供的用量也不会加长上下文 | 128000 |
        | `peer_serve` | 启动 TUI 时是否自动拉起 peer 服务| true |
        | `peer_tools` | 是否允许远端 peer 驱动本地工具 | true |
        | `peer_host` | peer 服务监听地址 | 0.0.0.0 |
        | `peer_port` | peer 服务监听端口| 1124 |
        | `extra` | 额外请求体列表，TUI 中可切换；如 `thinking` 开关 | [{"thinking": {"type": "enabled"}},{"thinking": {"type": "disabled"}}] |
    - [memory.json](./configure/memory.json)
      - 记忆持久化文件，你的所有会话都会存在这里面
    - [peer_identity.json](./configure/peer_identity.json)
      - 指纹隔离
    - [peer/](./configure/connect.json)
      - 连接持久化
  - [agentapi/](./agentapi)
    >agentapi是reSEN-Agent的核心，为上层提供了大模型操作设备和自我升级的基础
    - [meta_tools](./agentapi/meta_tools/)
      - `meta_tools`也叫做元工具，赋予了Agent最基础最核心的能力
        - [peer_agent.py](./agentapi/meta_tools/peer_agent.py)
          - `peer-agent`工具提供了连接到其它开启了`peer-api`端口的`reSEN-Agent`的能力
        - [tool_create.py](./agentapi/meta_tools/tool_create.py)
          - `tool-create`工具提供了*创建*热插拔工具的能力
        - [tool_delete.py](./agentapi/meta_tools/tool_delete.py)
          - `tool-delete`工具提供了*删除*热插拔工具的能力
        - [tool_run.py](./agentapi/meta_tools/tool_run.py)
          - `tool-run`工具提供了*运行*热插拔工具的能力
        - [tool_search.py](./agentapi/meta_tools/tool_search.py)
          - `tool-search`工具提供了*搜索和加载*热插拔工具的能力
        - [tool_skill.py](./agentapi/meta_tools/tool_skill.py)
          - `tool-skill`工具提供了与*技能*相关操作的能力
        - [tool_view.py](./agentapi/meta_tools/tool_view.py)
          - `tool-view`工具提供了*审查*热插拔工具代码的能力
    - [tools](./agentapi/tools/)
      - tools的正式名称为`hotplug-tools`，即`热插拔工具`，热插拔工具赋予了Agent执行任意操作的能力，并且可以按需定制
  - [api](./api/)
    >api包是远程控制的接口，为Agent间互相操作提供了网络通道
    - [peer_api.py](./api/peer_api.py)

## 作为开发者，我需要知道什么？
0. **看完所有readme.md**

## 安全提示

- 动态生成的工具能够具备`执行shell`、`SSH`、`桌面注入`、`网络请求`等高危能力；
- `peer_agent` 会把内容发送到指定主机并可能执行高危操作，只连接你信任的 peer；务必设置复杂的 `access_key`

## 许可证

[MIT](LICENSE)