# TimeAudit

- **这是什么：** 记录这台 Windows 电脑的硬件、进程、窗口使用和游戏帧率，供事后回看卡顿与使用时间。
- **我怎么用：** 打开本机 Grafana `http://localhost:43000` 看图；各面板怎么读见 [使用手册](使用手册.md)。
- **怎么知道正常：** 双击 `检查运行状态.vbs`，看采集、入库和备份是否都正常；图上没数据时，先检查选的时间范围。
- **坏了怎么提醒我：** 没有自动提醒，出问题直接跟 AI 说；看门狗会尝试恢复部分组件并留下状态记录。
- **让 AI 做什么：** 让 AI 按具体时间查卡顿或资源异常，结合采集覆盖与当时负载解释原因，不凭单个阈值定论。
- 安装、迁机和恢复见 [快速部署](快速部署.md)；代码结构见 [架构与数据流](ARCHITECTURE.md)，诊断边界见 [诊断操作](DIAGNOSTICS_OPERATIONS.md)。

网站电脑状态看板只维护 `grafana_provisioning/dashboards/website-computer-status.json`。
修改后在项目目录运行 `python -B generate_website_dashboards.py`，同批提交原定义与生成的
`website-computer-status-cpu-gpu.json`（面板 1–4）、`website-computer-status-memory-network.json`（面板 5–6）。
两组保留完整查询、样式和时间设置，只改变看板标识、标题及单列布局；不要单独编辑生成文件。
`python -B generate_website_dashboards.py --check` 只检查输出是否同步，不写文件。
既有 Grafana 文件预置入口加载这三份定义；文件预置不会启用外部共享，共享需通过现役管理员入口单独办理并回读。
