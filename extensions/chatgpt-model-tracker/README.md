# ChatGPT Model Tracker · 模型记录（实验版 0.1.0）

记录 ChatGPT 网页每次新对话请求中的模型字段，以及对应助手消息公开的模型字段。点浏览器扩展图标查看列表、导出 CSV / JSON；页面有可匹配的消息 ID 时也会显示标签。

## 安装（Chrome 111 或更新版本）

1. 解压压缩包，找到包含 `manifest.json` 的 `chatgpt-model-tracker` 文件夹。
2. 打开 Chrome，在地址栏输入 `chrome://extensions`。
3. 打开右上角「开发者模式」，点击「加载已解压的扩展程序」，选择上述文件夹。
4. **刷新已经打开的 ChatGPT 页面**，再发一条新消息。
5. 在工具栏拼图菜单中打开「ChatGPT Model Tracker · 模型记录」。可将其固定到工具栏。

不需要 API Key，不需要服务器，不需要 npm 安装。源代码全部随包提供。

## 如何理解结果

| 字段 | 含义 |
|---|---|
| Query 请求模型 / requested_model | 请求 JSON 的 `model` 字段；可能是 `auto` 或别名，并不证明实际使用了哪个模型。 |
| Response / response_model_slug | 相应助手消息的 `metadata.model_slug` 原始值。 |
| response_model | 助手消息的 `metadata.model` 原始值。 |
| message_model_slug / message_model | 助手消息顶层同名字段，单独保留来源。 |
| default_model_slug | 消息元数据中的默认模型字段，**不作为回复模型的替代值**。 |
| query_id / response_id | 关联提问与回复的消息 ID，不包含正文。继续生成、重新生成的请求可能没有 query_id。 |
| status | 采集状态；complete 仅表示流已读取完成，不证明存在模型字段或模型身份真实。 |

一个请求可关联多条助手消息（例如多个阶段或分支），CSV 中每条助手消息占一行；尚未识别到助手消息时保留请求行。JSON 保留一对多结构。CSV 空白表示没有观察到该字段，界面显示「未知」。同一提问 ID 多次请求时，页面标签显示最近一次，弹窗仍保留各次请求。

例如请求字段为 `auto`，回复的 `metadata.model_slug` 为 `example-model` 时，插件分别展示两者。`example-model` 仅为说明用名称，插件不内置模型名单或别名映射。

## 能力边界

- 这是 **公开字段观察器**，无法读取服务器内部路由、隐藏模型版本、权重或内部思考过程。不能保证检测每次请求的真实后台模型身份，也不会根据回答风格或模型自称推断身份。
- 当前只处理同源 fetch 的 `POST /backend-api/conversation` 和 `POST /backend-api/f/conversation`，支持 JSON、SSE 完整消息及部分显式元数据补丁。这是兼容假设，并非 OpenAI 对这些私有接口的稳定性承诺。
- 不补录安装之前的历史消息。WebSocket、Worker、XHR、语音、其他路径、某些工作模式或新版压缩增量格式可能无法记录。没出现任何请求时可能是页面未刷新，也可能是不支持的传输方式。
- 字段未公开时显示未知；缺失、失败或格式不兼容会保留可识别的请求并显示状态。SSE 单帧限制 2 MB、总复制量 32 MB；JSON 限制约 2 MB，超限停止采集。
- 只用消息 ID 匹配页面标签，不按消息顺序猜测；页面结构更改可能导致标签不显示，但弹窗记录仍可查看。
- 主页面脚本可干扰页面内观察结果，因此记录不适合作为安全审计或计费凭证。

## 数据与权限

仅有 `storage` 权限，内容脚本只在 `chatgpt.com` 和 `chat.openai.com` 上运行。为提取元数据会在内存中读取对话请求和响应副本，但不持久保存或导出聊天正文、附件、Cookie、认证令牌。插件不创建外部网络请求，不上传采集数据。模型字段、消息 ID、已有会话 ID、标签页 ID、时间及状态保存在本机扩展存储中，最多保留最近 300 次请求。该存储不额外加密，也不区分同一 Chrome 配置中的不同 ChatGPT 账号。

记录总量达到约 200 万 JSON 字符时也会优先删除最早记录，所以大批量采集时可能少于 300 次。「清空记录」删除所有已保存记录；尚在进行的已清空请求不会重新出现。卸载插件也会删除其扩展存储，但已下载的 CSV / JSON 需要自行删除。移除或停用后刷新 ChatGPT 页面，使页面钩子停止运行。

## 验证与开发

执行 `node --test tests/test.mjs`（Node 20+）运行模拟测试：覆盖 SSE 分块、CRLF、部分补丁、重复去重、未知值、多个回复、异常帧、CSV 转义及公式防护、fetch 副本不消耗原响应、并发存储和清空。

本版本已完成模拟测试和语法检查；尚未在你的已登录 ChatGPT / Chrome 中进行真实接口验收。首次安装后请确认新消息能出现记录。若始终未知，可仅提供字段名称和脱敏结构排查，不要提供 Cookie、Authorization 或完整 HAR。

人工验收建议：发送一条新消息，确认 Query 和 Response 关联；再试一次重新生成；导出 JSON / CSV 比对弹窗；清空后重新打开弹窗检查。

Chrome 官方文档：
- https://developer.chrome.com/docs/extensions/reference/manifest/content-scripts
- https://developer.chrome.com/docs/extensions/get-started/tutorial/hello-world

文件说明：`hook.js` 观察 fetch；`parser.js` 提取允许的字段；`content.js` 传递记录并显示标签；`background.js` 顺序写入本地存储；`popup.*` 提供查看与导出；`export.js` 生成 CSV。
